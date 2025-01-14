# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 CERN.
#
# CDS-ILS is free software; you can redistribute it and/or modify it under
# the terms of the MIT License; see LICENSE file for more details.

"""CDS-ILS Importer module."""
import time

import pkg_resources
from flask import current_app
from invenio_app_ils.errors import RecordHasReferencesError
from invenio_app_ils.proxies import current_app_ils
from invenio_app_ils.records_relations.api import RecordRelationsParentChild
from invenio_app_ils.relations.api import Relation
from invenio_db import db
from invenio_pidstore.models import PersistentIdentifier
from invenio_search import current_search

from cds_ils.importer.documents.importer import DocumentImporter
from cds_ils.importer.eitems.importer import EItemImporter
from cds_ils.importer.errors import UnknownProvider
from cds_ils.importer.series.importer import SeriesImporter


class Importer(object):
    """Importer class."""

    UPDATE_DOCUMENT_FIELDS = ("identifiers",)
    IS_PROVIDER_PRIORITY_SENSITIVE = False
    EITEM_OPEN_ACCESS = True
    EITEM_URLS_LOGIN_REQUIRED = True

    HELPER_METADATA_FIELDS = (
        "_eitem",
        "agency_code",
        "_serial",
        "provider_recid",
        "_migration",
    )

    def __init__(self, json_data, metadata_provider):
        """Constructor."""
        self.json_data = json_data
        self.metadata_provider = metadata_provider
        priority = current_app.config["CDS_ILS_IMPORTER_PROVIDERS"][
            metadata_provider
        ]["priority"]

        eitem_json_data = self._extract_eitems_json()
        document_importer_class = self.get_document_importer(metadata_provider)
        self.document_importer = document_importer_class(
            json_data,
            self.HELPER_METADATA_FIELDS,
            metadata_provider,
            self.UPDATE_DOCUMENT_FIELDS,
        )
        self.eitem_importer = EItemImporter(
            json_data,
            eitem_json_data,
            metadata_provider,
            priority,
            self.IS_PROVIDER_PRIORITY_SENSITIVE,
            self.EITEM_OPEN_ACCESS,
            self.EITEM_URLS_LOGIN_REQUIRED,
        )
        series_json = json_data.get("_serial", None)
        self.series_importer = SeriesImporter(series_json, metadata_provider)

        self.output_pid = None
        self.action = None
        self.partial_matches = []
        self.document = None
        self.ambiguous_matches = []
        self.series_list = []
        self.eitem_summary = {}
        self.fuzzy_matches = []

    def get_document_importer(self, provider, default=DocumentImporter):
        """Determine which document importer to use."""
        try:
            return pkg_resources.load_entry_point(
                "cds-ils", "cds_ils.document_importers", provider
            )
        except Exception:
            return default

    def _validate_provider(self):
        """Check if the chosen provider is matching the import data."""
        agency_code = self.json_data.get("agency_code")
        if not agency_code:
            raise UnknownProvider
        assert (
            self.json_data["agency_code"]
            == current_app.config["CDS_ILS_IMPORTER_PROVIDERS"][
                self.metadata_provider
            ]["agency_code"]
        )

    def _extract_eitems_json(self):
        """Extracts eitems json for given pre-processed JSON."""
        return self.json_data["_eitem"]

    def _match_document(self):
        """Search the catalogue for existing document."""
        document_class = current_app_ils.document_record_cls

        matching_pids = self.document_importer.search_for_matching_documents()
        if len(matching_pids) == 1:
            return document_class.get_record_by_pid(matching_pids[0])

        self.ambiguous_matches = matching_pids

        fuzzy_results = self.document_importer.fuzzy_match_documents()
        self.fuzzy_matches = [x.pid for x in fuzzy_results]

    def _serialize_partial_matches(self):
        """Serialize partial matches."""
        amibiguous_matches = [
            {"pid": match, "type": "ambiguous"} for match in
            self.ambiguous_matches]
        fuzzy_matches = [{"pid": match, "type": "fuzzy"} for match in
                         self.fuzzy_matches]
        return fuzzy_matches + amibiguous_matches

    def update_records(self, matched_document):
        """Update document eitem and series records."""
        self.document_importer.update_document(matched_document)
        self.eitem_importer.update_eitems(matched_document)
        self.eitem_summary = self.eitem_importer.summary()
        self.series_list = self.series_importer.import_series(matched_document)
        self.document = matched_document

    def delete_records(self, matched_document):
        """Deletes eitems records."""
        self.eitem_importer.delete_eitems(matched_document)
        self.eitem_summary = self.eitem_importer.summary()

    def index_all_records(self):
        """Index imported records."""
        document_indexer = current_app_ils.document_indexer
        series_indexer = current_app_ils.series_indexer
        eitem_indexer = current_app_ils.eitem_indexer

        eitem = self.eitem_importer.eitem_record
        if eitem:
            eitem_indexer.index(eitem)

        document_indexer.index(self.document)
        for series in self.series_list:
            series_indexer.index(series["series_record"])
        # give ES chance to catch up
        time.sleep(2)

    def import_record(self):
        """Import record."""
        self._validate_provider()

        # finds the exact match, update records
        matched_document = self._match_document()

        if matched_document:
            self.output_pid = matched_document["pid"]
            self.action = "update"
            self.update_records(matched_document)
            self.index_all_records()
            return self.import_summary()

        # finds the multiple matches or fuzzy matches, does not create new doc
        # requires manual intervention, to avoid duplicates
        if self.ambiguous_matches:
            return self.import_summary()

        self.document = self.document_importer.create_document()
        if self.document:
            self.output_pid = self.document["pid"]
            self.action = "create"
            self.eitem_importer.create_eitem(self.document)
            self.eitem_summary = self.eitem_importer.summary()
            self.series_list = self.series_importer.import_series(
                self.document)
            self.index_all_records()
        return self.import_summary()

    def delete_record(self):
        """Deletes the eitems of the record."""
        document_indexer = current_app_ils.document_indexer
        series_class = current_app_ils.series_record_cls
        self._validate_provider()
        self.action = "update"
        # finds the exact match, update records
        self.document = self._match_document()

        if self.document:
            self.output_pid = self.document["pid"]
            self.delete_records(self.document)
            current_search.flush_and_refresh(index="*")
            document_has_only_serial_relations = \
                len(self.document.relations.keys()) \
                and 'serial' in self.document.relations.keys()

            if not self.document.has_references() \
                    and document_has_only_serial_relations:

                # remove serial relations
                rr = RecordRelationsParentChild()
                serial_relations = self.document.relations.get('serial', [])
                relation_type = Relation.get_relation_by_name("serial")
                for relation in serial_relations:
                    serial = series_class.get_record_by_pid(
                        relation["pid_value"])
                    rr.remove(serial, self.document, relation_type)

            pid = self.document.pid
            # will fail if any relations / references present
            self.document.delete()
            # mark all PIDs as DELETED
            all_pids = PersistentIdentifier.query.filter(
                PersistentIdentifier.object_type == pid.object_type,
                PersistentIdentifier.object_uuid == pid.object_uuid,
            ).all()
            for rec_pid in all_pids:
                if not rec_pid.is_deleted():
                    rec_pid.delete()

            db.session.commit()
            document_indexer.delete(self.document)
            self.action = "delete"

        return self.import_summary()

    def import_summary(self):
        """Provide import summary."""
        doc_json = {}
        if self.document:
            doc_json = self.document.dumps()
        return {
            "output_pid": self.output_pid,
            "action": self.action,
            "partial_matches": self._serialize_partial_matches(),
            "eitem": self.eitem_summary,
            "series": self.series_list,
            "raw_json": self.json_data,
            "document_json": doc_json,
            "document": self.document
        }

    def preview_delete(self):
        """Preview deleting a record."""
        self._validate_provider()
        self.action = "update"
        # finds the exact match, update records
        self.document = self._match_document()

        if self.document:
            self.output_pid = self.document["pid"]
            self.eitem_summary = \
                self.eitem_importer.preview_delete(self.document)
            document_has_only_serial_relations = \
                len(self.document.relations.keys()) \
                and 'serial' in self.document.relations.keys()
            if not self.document.has_references() \
                    and document_has_only_serial_relations:
                self.action = "delete"

        return self.import_summary()

    def preview_import(self):
        """Previews the record import."""
        self._validate_provider()
        self.document = self._match_document()
        self.eitem_summary = self.eitem_importer.preview_import(self.document)
        self.series_list = self.series_importer.preview_import_series()

        if self.document:
            self.document = self.document_importer.preview_document_update(
                self.document)
            self.action = "update"
            self.output_pid = self.document["pid"]
        else:
            self.document = self.document_importer.preview_document_import()
            self.action = "create"

        # finds the multiple matches or fuzzy matches, does not create new doc
        # requires manual intervention, to avoid duplicates
        if self.ambiguous_matches:
            self.action = None

        return self.import_summary()
