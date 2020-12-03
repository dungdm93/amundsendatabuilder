# Copyright Contributors to the Amundsen project.
# SPDX-License-Identifier: Apache-2.0

import logging
from collections import namedtuple

from pyhocon import ConfigFactory, ConfigTree
from typing import Iterator, Union, Dict, Any

from databuilder import Scoped
from databuilder.extractor.base_extractor import Extractor
from databuilder.extractor.sql_alchemy_extractor import SQLAlchemyExtractor
from databuilder.models.table_metadata import TableMetadata, ColumnMetadata
from itertools import groupby


TableKey = namedtuple('TableKey', ['schema', 'table_name'])

LOGGER = logging.getLogger(__name__)


class MysqlMetadataExtractor(Extractor):
    """
    Extracts mysql table and column metadata from underlying meta store database using SQLAlchemyExtractor
    """
    # SELECT statement from mysql information_schema to extract table and column metadata
    SQL_STATEMENT = """
        SELECT
            lower(c.COLUMN_NAME) AS col_name,
            c.COLUMN_COMMENT AS col_description,
            lower(c.DATA_TYPE) AS col_type,
            lower(c.ORDINAL_POSITION) AS col_sort_order,
            {cluster_source} AS cluster,
            lower(c.TABLE_SCHEMA) AS "schema",
            lower(c.TABLE_NAME) AS name,
            t.TABLE_COMMENT AS description,
            CASE WHEN lower(t.TABLE_TYPE) = "view" 
                THEN "true"
                ELSE "false"
            END AS is_view
        FROM
            INFORMATION_SCHEMA.COLUMNS AS c
        LEFT JOIN INFORMATION_SCHEMA.TABLES t 
            ON	c.TABLE_NAME = t.TABLE_NAME
            AND c.TABLE_SCHEMA = t.TABLE_SCHEMA
        {where_clause_suffix}
        ORDER BY
            cluster,
            "schema",
            name,
            col_sort_order;
    """

    # CONFIG KEYS
    WHERE_CLAUSE_SUFFIX_KEY = 'where_clause_suffix'
    CLUSTER_KEY = 'cluster_key'
    USE_CATALOG_AS_CLUSTER_NAME = 'use_catalog_as_cluster_name'
    DATABASE_KEY = 'database_key'

    # Default values
    DEFAULT_CLUSTER_NAME = 'master'

    DEFAULT_CONFIG = ConfigFactory.from_dict(
        {WHERE_CLAUSE_SUFFIX_KEY: ' ', CLUSTER_KEY: DEFAULT_CLUSTER_NAME, USE_CATALOG_AS_CLUSTER_NAME: True}
    )

    def init(self, conf: ConfigTree) -> None:
        conf = conf.with_fallback(MysqlMetadataExtractor.DEFAULT_CONFIG)
        self._cluster = conf.get_string(MysqlMetadataExtractor.CLUSTER_KEY)

        if conf.get_bool(MysqlMetadataExtractor.USE_CATALOG_AS_CLUSTER_NAME):
            cluster_source = "c.TABLE_SCHEMA"
        else:
            cluster_source = "'{}'".format(self._cluster)

        self._database = conf.get_string(MysqlMetadataExtractor.DATABASE_KEY, default='mysql')

        self.sql_stmt = MysqlMetadataExtractor.SQL_STATEMENT.format(
            where_clause_suffix=conf.get_string(MysqlMetadataExtractor.WHERE_CLAUSE_SUFFIX_KEY),
            cluster_source=cluster_source
        )

        self._alchemy_extractor = SQLAlchemyExtractor()
        sql_alch_conf = Scoped.get_scoped_conf(conf, self._alchemy_extractor.get_scope())\
            .with_fallback(ConfigFactory.from_dict({SQLAlchemyExtractor.EXTRACT_SQL: self.sql_stmt}))

        self.sql_stmt = sql_alch_conf.get_string(SQLAlchemyExtractor.EXTRACT_SQL)

        LOGGER.info('SQL for mysql metadata: {}'.format(self.sql_stmt))

        self._alchemy_extractor.init(sql_alch_conf)
        self._extract_iter: Union[None, Iterator] = None

    def extract(self) -> Union[TableMetadata, None]:
        if not self._extract_iter:
            self._extract_iter = self._get_extract_iter()
        try:
            return next(self._extract_iter)
        except StopIteration:
            return None

    def get_scope(self) -> str:
        return 'extractor.mysql_metadata'

    def _get_extract_iter(self) -> Iterator[TableMetadata]:
        """
        Using itertools.groupby and raw level iterator, it groups to table and yields TableMetadata
        :return:
        """
        for key, group in groupby(self._get_raw_extract_iter(), self._get_table_key):
            columns = []

            for row in group:
                last_row = row
                columns.append(ColumnMetadata(row['col_name'], row['col_description'],
                                              row['col_type'], row['col_sort_order']))

            yield TableMetadata(self._database, last_row['cluster'],
                                last_row['schema'],
                                last_row['name'],
                                last_row['description'],
                                columns,
                                is_view=last_row['is_view'])

    def _get_raw_extract_iter(self) -> Iterator[Dict[str, Any]]:
        """
        Provides iterator of result row from SQLAlchemy extractor
        :return:
        """
        row = self._alchemy_extractor.extract()
        while row:
            yield row
            row = self._alchemy_extractor.extract()

    def _get_table_key(self, row: Dict[str, Any]) -> Union[TableKey, None]:
        """
        Table key consists of schema and table name
        :param row:
        :return:
        """
        if row:
            return TableKey(schema=row['schema'], table_name=row['name'])

        return None
