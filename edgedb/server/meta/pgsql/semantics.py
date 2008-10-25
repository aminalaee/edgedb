from __future__ import with_statement

from psycopg2 import ProgrammingError

from semantix.lib.caos import DomainClass, ConceptClass, ConceptAttributeType, MetaError, ConceptLinkType

from .datasources.introspection.table import *
from .datasources.meta.concept import *
from .common import DatabaseTable
from ...data.pgsql import EntityTable

class ConceptTable(DatabaseTable):
    def create(self):
        """
            CREATE TABLE "caos"."concept"(
                id serial NOT NULL,
                name text NOT NULL,

                PRIMARY KEY (id)
            )
        """
        super(ConceptTable, self).create()

    def insert(self, *dicts, **kwargs):
        """
            INSERT INTO "caos"."concept"(name) VALUES (%(name)s) RETURNING id
        """
        super(ConceptTable, self).insert(*dicts, **kwargs)

class ConceptMapTable(DatabaseTable):
    def create(self):
        """
            CREATE TABLE "caos"."concept_map"(
                id serial NOT NULL,
                source_id integer NOT NULL,
                target_id integer NOT NULL,
                link_type varchar(255) NOT NULL,
                mapping char(2) NOT NULL,

                PRIMARY KEY (id),
                FOREIGN KEY (source_id) REFERENCES "caos"."concept"(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES "caos"."concept"(id) ON DELETE CASCADE
            )
        """
        super(ConceptMapTable, self).create()

    def insert(self, *dicts, **kwargs):
        """
            INSERT INTO "caos"."concept_map"(source_id, target_id, link_type, mapping)
                VALUES (
                            (SELECT id FROM caos.concept WHERE name = %(source)s),
                            (SELECT id FROM caos.concept WHERE name = %(target)s),
                            %(link_type)s,
                            %(mapping)s
                ) RETURNING id
        """
        super(ConceptMapTable, self).insert(*dicts, **kwargs)

class EntityMapTable(DatabaseTable):
    def create(self):
        """
            CREATE TABLE "caos"."entity_map"(
                source_id integer NOT NULL,
                target_id integer NOT NULL,
                link_type_id integer NOT NULL,
                weight integer NOT NULL,

                PRIMARY KEY (source_id, target_id, link_type_id),
                FOREIGN KEY (source_id) REFERENCES "caos"."entity"(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES "caos"."entity"(id) ON DELETE CASCADE,
                FOREIGN KEY (link_type_id) REFERENCES "caos"."concept_map"(id) ON DELETE RESTRICT
            )
        """
        super(EntityMapTable, self).create()


class MetaDataIterator(object):
    def __init__(self, helper):
        self.helper = helper
        self.iter = iter(helper.concepts)

    def __iter__(self):
        return self

    def next(self):
        concept = next(self.iter)
        return ConceptClass(concept, meta_backend=self.helper.meta_backend)


class MetaBackendHelper(object):

    def __init__(self, connection, meta_backend):
        self.connection = connection
        self.meta_backend = meta_backend
        self.domain_helper = self.meta_backend.domain_backend
        self.concepts = dict((self.demangle_name(t['name']), t) for t in TableList.fetch(schema_name='caos'))
        self.concept_table = ConceptTable(self.connection)
        self.concept_table.create()
        self.concept_map_table = ConceptMapTable(self.connection)
        self.concept_map_table.create()
        EntityTable(self.connection).create()
        self.entity_map_table = EntityMapTable(self.connection)
        self.entity_map_table.create()

    def demangle_name(self, name):
        if name.endswith('_data'):
            name = name[:-5]

        return name


    def mangle_name(self, name, quote=False):
        if quote:
            return '"caos"."%s_data"' % name
        else:
            return 'caos.%s_data' % name


    def load(self, name):
        if name not in self.concepts:
            raise MetaError('reference to an undefined concept "%s"' % name)

        bases = ()
        dct = {'name': name}

        columns = TableColumns.fetch(table_name=self.mangle_name(name))
        attributes = {}
        for row in columns:
            if row['column_name'] == 'entity_id':
                continue

            domain = self.domain_helper.domain_from_pg_type(row['column_type'], name + '__' + row['column_name'])

            try:
                attr = ConceptAttributeType(domain, row['column_required'], row['column_default'])
                attributes[row['column_name']] = attr
            except MetaError, e:
                print e

        dct['attributes'] = attributes

        dct['links'] = {}
        for r in ConceptLinks.fetch(source_concept=name):
            l = ConceptLinkType(r['source_concept'], r['target_concept'], r['link_type'], r['mapping'])
            dct['links'][(r['link_type'], r['target_concept'])] = l

        dct['rlinks'] = {}
        for r in ConceptLinks.fetch(target_concept=name):
            l = ConceptLinkType(r['source_concept'], r['target_concept'], r['link_type'], r['mapping'])
            dct['rlinks'][(r['link_type'], r['source_concept'])] = l

        inheritance = TableInheritance.fetch(table_name=self.mangle_name(name))
        inheritance = [i[0] for i in inheritance[1:]]

        if len(inheritance) > 0:
            for table in inheritance:
                bases += (ConceptClass(self.demangle_name(table), meta_backend=self.meta_backend),)

        return bases, dct


    def store(self, cls, phase):
        try:
            current = ConceptClass(cls.name, meta_backend=self.meta_backend)
        except MetaError:
            current = None

        if current is None or phase == 2:
            self.create_concept(cls, phase)


    def create_concept(self, cls, phase):
        if phase is None or phase == 1:
            concept = self.concept_table.insert(name=cls.name)

            qry = 'CREATE TABLE %s' % self.mangle_name(cls.name, True)

            columns = []

            for attr_name in sorted(cls.attributes.keys()):
                attr = cls.attributes[attr_name]
                column_type = self.domain_helper.pg_type_from_domain(attr.domain)
                column = '"%s" %s %s' % (attr_name, column_type, 'NOT NULL' if attr.required else '')
                columns.append(column)

            qry += '(entity_id integer NOT NULL REFERENCES caos.entity(id) ON DELETE CASCADE, ' + ','.join(columns) + ')'

            if len(cls.parents) > 0:
                qry += ' INHERITS (' + ','.join([self.mangle_name(p, True) for p in cls.parents]) + ')'

            with self.connection as cursor:
                cursor.execute(qry)

        if phase is None or phase == 2:
            for link in cls.links.values():
                self.concept_map_table.insert(source=link.source, target=link.target,
                                              link_type=link.link_type, mapping=link.mapping)

    def __iter__(self):
        return MetaDataIterator(self)
