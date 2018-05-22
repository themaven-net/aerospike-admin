# Copyright 2013-2018 Aerospike, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import types
from collections import OrderedDict
from itertools import groupby
from operator import itemgetter

from lib.view import terminal

from .. import decl
from .render_utils import Aggregator, ErrorEntry, NoEntry


class BaseRSheet(object):
    def __init__(self, sheet, title, sources, common, description=None):
        """
        Arguments:
        sheet       -- The decl.sheet to render.
        title       -- Title for this render.
        data_source -- Dictionary of data-sources to project fields from.

        Keyword Arguments:
        sheet_style -- 'SheetStyle.columns': Show sheet where records are
                                             represented as rows.
                       'SheetStyle.json'   : Show sheet represented as JSON.
        common      -- A dict of common information passed to each entry.
        description -- A description of the sheet.
        """
        self.decl = sheet
        self.title = title

        self._init_sources(sources)

        self.common = common
        self.description = description

        projections = self.project_fields()
        projections = self.where(projections)
        projections_groups = self.group_by_fields(projections)
        projections_groups = self.order_by_fields(projections_groups)
        self.rfields = self.create_rfields(projections_groups)
        self.visible_rfields = [rfield for rfield in self.rfields
                                if not rfield.hidden]

        for rfield in self.rfields:
            rfield.prepare()

    # ==========================================================================
    # Required overrides.

    def do_render(self):
        """
        Renders the data in the style defined by the RSheet class.
        """
        raise NotImplementedError('override')

    def do_create_tuple_field(self, field, groups):
        """
        Each RSheet may define custom versions of RTupleField.

        Arguments:
        field  -- The decl.TupleField describing this tuple field.
        groups -- The data sources having already been processed into groups.
        """
        raise NotImplementedError('override')

    def do_create_field(self, field, groups, parent_key=None):
        """
        Each RSheet may define custom version of RFields.
        Arguments:
        field  -- The decl.Field describing this field.
        groups -- The data sources having already been processed into groups.

        Keyword Arguments:
        parent_key -- If this field is the child of a TupleField, then this is
                      the key defined within that TupleField.
        """
        raise NotImplementedError('override')

    # ==========================================================================
    # Other methods.

    def _init_sources(self, sources):
        # This assertion can fire when a node is leaving/joining and some
        # commands on a subset of the nodes. Should this event be logged?
        # n_source_records = map(len, sources.itervalues())

        # assert len(set(n_source_records)) == 1, \
        #     "sources contain different numbers of records {}".format(
        #         zip(sources.keys(), n_source_records))

        source_keys = set(keys for d in sources.itervalues()
                          for keys in d.iterkeys())

        # Change sources from: {'source':{'row_key':value}}
        #                  to: [{'source':value}]
        converted_sources = []

        for row_key in source_keys:
            new_source = {}
            converted_sources.append(new_source)

            for source, value in sources.iteritems():
                new_source[source] = value[row_key]

        # Expand for_each
        expanded_sources = []

        for source in converted_sources:
            if not self.decl.for_each:
                expanded_sources.append(source)
                continue

            for for_each in self.decl.for_each:
                sub_source = source[for_each]

                try:
                    for value in sub_source.iteritems():
                        new_source = source.copy()
                        new_source[for_each] = value
                        expanded_sources.append(new_source)
                except AttributeError:
                    # Non-iterable - probably an Exception.
                    new_source = source.copy()
                    new_source[for_each] = ErrorEntry
                    expanded_sources.append(new_source)

        self.sources = expanded_sources

        self.n_records = len(expanded_sources)

    def render(self):
        # XXX - Could be useful to pass 'group_by' and 'order_by' into the render
        #       function. Could use the decl's copy as their defaults.
        return self.do_render()

    def project_fields(self):
        projections = []

        for sources in self.sources:
            projection = OrderedDict()
            projections.append(projection)

            for dfield in self.decl.fields:
                self._project_field(dfield, sources, projection)

        return projections

    def _project_field(self, dfield, sources, projection):
        if isinstance(dfield, decl.TupleField):
            child_projections = OrderedDict()
            projection[dfield.key] = child_projections

            for child_dfield in dfield.fields:
                self._project_field(child_dfield, sources, child_projections)

            return

        try:
            entry = dfield.projector(self.decl, sources)
        except decl.NoEntryException:
            entry = NoEntry
        except decl.ErrorEntryException:
            entry = ErrorEntry

        projection[dfield.key] = entry

    def where(self, projections):
        if self.decl.where:
            where_fn = self.decl.where

            for record_ix in xrange(len(projections) - 1, -1, -1):
                if not where_fn(projections[record_ix]):
                    del projections[record_ix]

        return projections

    def group_by_fields(self, projections):
        """
        Single or composite key grouping
        """
        # XXX - Allow 'group by' on a field within a TupleField.

        grouping = (((), projections),)
        group_bys = self.decl.group_bys

        if group_bys is None:
            return OrderedDict(grouping)

        if isinstance(group_bys, types.StringType):
            group_bys = (group_bys,)

        for group_by in group_bys:
            next_grouping = []

            for pkey, pgroup in grouping:
                pgroup_sort = sorted(
                    pgroup, key=itemgetter(group_by))
                cgroups = [(pkey + (ckey,), list(cgroup)) for ckey, cgroup
                           in groupby(
                               pgroup_sort,
                               key=itemgetter(group_by))]

                next_grouping.extend(cgroups)

            grouping = next_grouping

        return OrderedDict(grouping)

    def order_by_fields(self, projections_groups):
        # XXX - Allow 'order by' on a field within a TupleField.
        # XXX - Allow desc order.

        order_bys = self.decl.order_bys

        if order_bys is None:
            return projections_groups

        for projections_group in projections_groups.values():
            for order_by in order_bys[::-1]:
                projections_group.sort(key=itemgetter(order_by))

        return projections_groups

    def create_rfields(self, projections_groups):
        groups = projections_groups.values()

        return [self.create_rfield(field, groups)
                for field in self.decl.fields]

    def create_rfield(self, field, groups, parent_key=None):
        if isinstance(field, decl.TupleField):
            return self.do_create_tuple_field(field, groups)

        return self.do_create_field(field, groups, parent_key=parent_key)


class BaseRTupleField(object):
    def __init__(self, rsheet, field, groups):
        """
        Arguments:
        rsheet -- BaseRSheet being rendered.
        field  -- decl.TupleField.
        groups -- Sequence of sub-sequences where each sub-sequence is a group
                  determined by 'rsheet.decl.group_bys'.
        """
        self.rsheet = rsheet
        self.decl = field
        self.parent_key = None
        self.n_groups = len(groups)

        self._init_as_tuple_field(groups)

    # ==========================================================================
    # Optional overrides.

    def do_prepare(self):
        """
        Post processing phase after all fields in the RSheet have been
        initialized.
        """
        return  # Override if as needed.

    # ==========================================================================
    # Other methods.

    def _init_as_tuple_field(self, groups):
        self.is_tuple_field = True
        self.subfields = [
            self.rsheet.do_create_field(
                subdecl, groups, parent_key=self.decl.key)
            for subdecl in self.decl.fields]
        self.visible = [subfield for subfield in self.subfields if not subfield.hidden]
        self.hidden = not self.visible

    def prepare(self):
        if self.hidden:
            return

        for subfield in self.subfields:
            subfield.prepare()

        self.do_prepare()

    def has_aggregate(self):
        return any(sub.has_aggregate() for sub in self.visible)

    def get_kv(self, group_ix, entry_ix):
        return self.decl.key, dict(
            sub.get_kv(group_ix, entry_ix) for sub in self.visible)

    def n_entries_in_group(self, group_ix):
        return self.subfields[0].n_entries_in_group(group_ix)


class BaseRField(object):
    def __init__(self, rsheet, field, groups, parent_key=None):
        """
        Arguments:
        rsheet -- BaseRSheet being rendered.
        field  -- 'decl.TupleField'.
        groups -- Sequence of sub-sequences where each sub-sequence is a group
                  determined by 'rsheet.decl.group_bys'.

        Keyword Argument:
        parent_key -- Not None: the decl.key value for the parent 'TupleField'.
        """
        self.rsheet = rsheet
        self.decl = field
        self.parent_key = parent_key
        self.n_groups = len(groups)

        if self.rsheet.decl.group_bys:
            self.is_grouped_by = (self.decl.key in self.rsheet.decl.group_bys)
        else:
            self.is_grouped_by = False

        if self.rsheet.decl.order_bys:
            self.is_ordered_by = (self.decl.key in self.rsheet.decl.order_bys)
        else:
            self.is_ordered_by = False

        self._init_as_field(groups)

    # ==========================================================================
    # Optional overrides.

    def do_prepare(self):
        """
        Post processing phase after all fields in the RSheet have been
        initialized.
        """
        return  # Override as needed.

    # ==========================================================================
    # Other methods.

    def _init_as_field(self, raw_groups):
        self.is_tuple_field = False

        self.groups = []
        self.groups_converted = []

        self.aggregates = []
        self.aggregates_converted = []

        self._init_load_groups(raw_groups)

        for group in self.groups:
            self._init_aggregate_group(group)

    def _init_load_groups(self, raw_groups):
        field_key = self.decl.key

        if self.parent_key:
            self.groups = [map(lambda g: g[self.parent_key][field_key], raw_group)
                           for raw_group in raw_groups]
        else:
            self.groups = [map(itemgetter(field_key), raw_group)
                           for raw_group in raw_groups]

        # Determine if hidden.
        if self.decl.hidden is None:
            self.hidden = not any(
                v is not NoEntry for group in self.groups for v in group)
        else:
            self.hidden = self.decl.hidden

    def _init_aggregate_group(self, group):
        if self.hidden:
            # Do not need to aggregate hidden fields.
            self.aggregates.append(None)
            self.aggregates_converted.append('')
            return

        if self.decl.aggregator is None:
            if self.is_grouped_by and self.rsheet.decl.has_aggregates:
                # If a grouped field doesn't have an aggregator then the grouped
                # value will appear in the aggregates line.
                self.aggregates.append(group[0])

                if group[0] is ErrorEntry:
                    self.aggregates_converted.append(self.rsheet.decl.error_entry)
                elif group[0] is NoEntry:
                    self.aggregates_converted.append(self.rsheet.decl.no_entry)
                else:
                    self.aggregates_converted.append(str(group[0]))
            else:
                self.aggregates.append(None)
                self.aggregates_converted.append('')
            return

        if any(e is ErrorEntry for e in group):
            aggregate_value = ErrorEntry
        else:
            group_entries = [e for e in group if e is not NoEntry]
            aggregate_value = Aggregator(
                self.decl.aggregator, group_entries).result

            if aggregate_value is None:
                aggregate_value = NoEntry

        self.aggregates.append(aggregate_value)

    def prepare(self):
        if self.hidden:
            return

        self._prepare_entry_data()
        self._prepare_convert()
        self.do_prepare()

    def _prepare_entry_data(self):
        self.groups_entry_data = []

        for group_ix, group in enumerate(self.groups):
            entry_edata = []
            self.groups_entry_data.append(entry_edata)
            entries = [self.entry_value(e) for e in group]

            for entry_ix, entry in enumerate(entries):
                record = dict((rfield.get_kv(group_ix, entry_ix)
                               for rfield in self.rsheet.rfields))

                entry_edata.append(
                    decl.EntryData(
                        value=entry,
                        values=entries,
                        record=record,
                        common=self.rsheet.common,
                        is_error=group[entry_ix] is ErrorEntry))

    def _prepare_convert(self):
        self._prepare_convert_groups()
        self._prepare_convert_aggregates()

    def _prepare_convert_groups(self):
        self.groups_converted = []

        for fgroup in self.groups_entry_data:
            group_converted = []
            self.groups_converted.append(group_converted)

            for edata in fgroup:
                if edata.value is None:
                    if edata.is_error:
                        group_converted.append(self.rsheet.decl.error_entry)
                    else:
                        group_converted.append(self.rsheet.decl.no_entry)
                else:
                    group_converted.append(str(self.decl.converter(edata)))

    def _prepare_convert_aggregates(self):
        if self.decl.aggregator is None:
            return

        self.aggregates_converted = []

        if self.decl.aggregator.converter is None:
            converter = self.decl.converter
        else:
            converter = self.decl.aggregator.converter

        for aggr_ix, aggregate in enumerate(self.aggregates):
            if aggregate is None:
                self.aggregates_converted.append('')
            elif aggregate is NoEntry:
                self.aggregates_converted.append(self.rsheet.decl.no_entry)
            elif aggregate is ErrorEntry:
                self.aggregates_converted.append(self.rsheet.decl.error_entry)
            else:
                self.aggregates_converted.append(
                    converter(decl.EntryData(value=aggregate)))

    def entry_value(self, entry):
        if entry is ErrorEntry or entry is NoEntry:
            return None

        return entry

    def get_kv(self, group_ix, entry_ix):
        entry = self.groups[group_ix][entry_ix]
        return self.decl.key, self.entry_value(entry)

    def has_aggregate(self):
        return self.decl.aggregator is not None

    def n_entries_in_group(self, group_ix):
        return len(self.groups[group_ix])

    def entry_format(self, group_ix, entry_ix):
        """
        Arguments:
        group_ix -- Index of a group in self.groups.
        entry_ix -- Index of an entry within a group.

        Return:
        Tuple of form (string_alert, format_function). The string_alert can be
        used when sheet is displayed in a plain text rendering (currently only
        for testing format).
        """
        edata = self.groups_entry_data[group_ix][entry_ix]

        if edata.is_error:
            return None, lambda v: terminal.fg_magenta() + v + terminal.fg_not_magenta()

        for name, formatter in self.decl.formatters:
            format_fn = formatter(edata)

            if format_fn is not None:
                return name, format_fn

        return None, None
