import re
import yaml
from collections import defaultdict
import os


def run_sql(sql, portid, con_args):
    """
    @brief Wrapper function for ____run_sql_query
    """
    from madpack import ____run_sql_query
    return ____run_sql_query(sql, True, portid, con_args)


def get_signature_for_compare(schema, proname, rettype, argument):
    """
    @brief Get the signature of a UDF/UDA for comparison
    """
    signature = '{0} {1}.{2}({3})'.format(rettype.strip(), schema.strip(),
                                          proname.strip(), argument.strip())
    signature = re.sub('"', '', signature)
    return signature.lower()


class UpgradeBase:
    """
    @brief Base class for handling the upgrade
    """
    def __init__(self, schema, portid, con_args):
        self._schema = schema.lower()
        self._portid = portid
        self._con_args = con_args
        self._schema_oid = None
        self._get_schema_oid()

    """
    @brief Wrapper function for run_sql
    """
    def _run_sql(self, sql):
        return run_sql(sql, self._portid, self._con_args)

    """
    @brief Get the oids of some objects from the catalog in the current version
    """
    def _get_schema_oid(self):
        self._schema_oid = self._run_sql("""
            SELECT oid FROM pg_namespace WHERE nspname = '{schema}'
            """.format(schema=self._schema))[0]['oid']

    def _get_function_info(self, oid):
        """
        @brief Get the function name, return type, and arguments given an oid
        @note The function can only handle the case that proallargtypes is null,
        refer to pg_catalog.pg_get_function_identity_argument and
        pg_catalog.pg_get_function_result in PG for a complete implementation, which are
        not supported by GP
        """
        row = self._run_sql("""
            SELECT
                max(proname) AS proname,
                max(rettype) AS rettype,
                array_to_string(
                    array_agg(argname || ' ' || argtype order by i), ',') AS argument
            FROM
            (
                SELECT
                    proname,
                    textin(regtypeout(prorettype::regtype)) AS rettype,
                    CASE array_upper(proargtypes,1) WHEN -1 THEN ''
                        ELSE textin(regtypeout(unnest(proargtypes)::regtype))
                    END AS argtype,
                    CASE WHEN proargnames IS NULL THEN ''
                        ELSE unnest(proargnames)
                    END AS argname,
                    CASE array_upper(proargtypes,1) WHEN -1 THEN 1
                        ELSE generate_series(0, array_upper(proargtypes, 1))
                    END AS i
                FROM
                    pg_proc AS p
                WHERE
                    oid = {oid}
            ) AS f
            """.format(oid=oid))
        return {"proname": row[0]['proname'],
                "rettype": row[0]['rettype'],
                "argument": row[0]['argument']}


class ChangeHandler(UpgradeBase):
    """
    @brief This class reads changes from the configuration file and handles
    the dropping of objects
    """
    def __init__(self, schema, portid, con_args, maddir, mad_dbrev):
        UpgradeBase.__init__(self, schema, portid, con_args)
        self._opr_ind_svec = None
        self._get_opr_indepent_svec()
        self._maddir = maddir
        self._mad_dbrev = mad_dbrev
        self._newmodule = None
        self._udt = None
        self._udf = None
        self._uda = None
        self._udc = None
        self._load()

    def _get_opr_indepent_svec(self):
        """
        @brief Get the User Defined Operators independent of svec in the current version
        """
        rows = self._run_sql("""
            SELECT
                oprname,
                tl.typname AS typ_left,
                nspl.nspname AS nsp_left,
                tr.typname AS typ_right,
                nspr.nspname AS nsp_right
            FROM
                pg_operator AS o,
                pg_type AS tl,
                pg_type AS tr,
                pg_namespace AS nspl,
                pg_namespace AS nspr
            WHERE
                oprnamespace = {schema_oid} AND
                (
                    oprleft <> '{schema_madlib}.svec'::regtype AND
                    oprright <> '{schema_madlib}.svec'::regtype AND
                    oprleft = tl.oid AND
                    oprright = tr.oid AND
                    tl.typnamespace = nspl.oid AND
                    tr.typnamespace = nspr.oid
                )
            """.format(schema_madlib=self._schema, schema_oid=self._schema_oid))
        self._opr_ind_svec = {}
        for row in rows:
            self._opr_ind_svec[row['oprname']] = row

    def _load_config_param(self, config_iterable):
        """
        Replace schema_madlib with the appropriate schema name and
        make all function names lower case to ensure ease of comparison.

        Args:
            @param config_iterable is an iterable of dictionaries, each with
                        key = object name (eg. function name) and value = details
                        for the object. The details for the object are assumed to
                        be in a dictionary with following keys:
                            rettype: Return type
                            argument: List of arguments

        Returns:
            A dictionary that lists all specific objects (functions, aggregates, etc)
            with object name as key and a list as value, where the list
            contains all the items present in

            another dictionary with objects details
            as the value.
        """
        _return_obj = defaultdict(list)
        if config_iterable is not None:
            for each_config in config_iterable:
                for obj_name, obj_details in each_config.iteritems():
                    rettype = obj_details['rettype'].lower().replace(
                                                'schema_madlib', self._schema)
                    if obj_details['argument'] is not None:
                        argument = obj_details['argument'].lower().replace(
                                                'schema_madlib', self._schema)
                        all_arguments = [each_arg.strip()
                                         for each_arg in argument.split(',')]
                    _return_obj[obj_name].append(
                                    {'rettype': rettype,
                                     'argument': ','.join(all_arguments)})
        return _return_obj

    def _load(self):
        """
        @brief Load the configuration file
        """
        # _mad_dbrev = 1.0
        if float(self._mad_dbrev) < 1.1:
            filename = os.path.join(self._maddir, 'madpack',
                                    'changelist_1.0_1.4.yaml')
        # _mad_dbrev = 1.1
        elif float(self._mad_dbrev) < 1.2:
            filename = os.path.join(self._maddir, 'madpack',
                                    'changelist_1.1_1.4.yaml')
        # _mad_dbrev = 1.2
        elif float(self._mad_dbrev) < 1.3:
            filename = os.path.join(self._maddir, 'madpack',
                                    'changelist_1.2_1.4.yaml')
        else:
            filename = os.path.join(self._maddir, 'madpack',
                                    'changelist.yaml')

        config = yaml.load(open(filename))

        self._newmodule = config['new module'] if config['new module'] else {}
        self._udt = config['udt'] if config['udt'] else {}
        self._udc = config['udc'] if config['udc'] else {}
        self._udf = self._load_config_param(config['udf'])
        self._uda = self._load_config_param(config['uda'])

    @property
    def newmodule(self):
        return self._newmodule

    @property
    def udt(self):
        return self._udt

    @property
    def uda(self):
        return self._uda

    @property
    def udf(self):
        return self._udf

    @property
    def udc(self):
        return self._udc

    def get_udf_signature(self):
        """
        @brief Get the list of UDF signatures for comparison
        """
        res = defaultdict(bool)
        for udf in self._udf:
            for item in self._udf[udf]:
                signature = get_signature_for_compare(
                    self._schema, udf, item['rettype'], item['argument'])
                res[signature] = True
        return res

    def get_uda_signature(self):
        """
        @brief Get the list of UDA signatures for comparison
        """
        res = defaultdict(bool)
        for uda in self._uda:
            for item in self._uda[uda]:
                signature = get_signature_for_compare(
                    self._schema, uda, item['rettype'], item['argument'])
                res[signature] = True
        return res

    def drop_changed_udt(self):
        """
        @brief Drop all types that were updated/removed in the new version
        @note It is dangerous to drop a UDT becuase there might be many
        dependencies
        """
        # Note that we use CASCADE option here. This might be dangerous because
        # it may drop some undetected dependent objects (eg. UDCast, UDOp, etc)
        for udt in self._udt:
            self._run_sql("DROP TYPE IF EXISTS {0}.{1} CASCADE".
                          format(self._schema, udt))
            if udt == 'svec':
                # Drop operators defined in the svec module which do not
                # depend on svec. We will run the whole svec.sql without
                # filtering once svec changed
                for opr in self._opr_ind_svec:
                    self._run_sql("""
                        DROP OPERATOR IF EXISTS {schema}.{oprname}
                        ({nsp_left}.{typ_left}, {nsp_left}.{typ_left})
                        """.format(
                            schema=self._schema, oprname=opr,
                            nsp_left=self._opr_ind_svec[opr]['nsp_left'],
                            typ_left=self._opr_ind_svec[opr]['typ_left'],
                            nsp_right=self._opr_ind_svec[opr]['nsp_right'],
                            typ_right=self._opr_ind_svec[opr]['typ_right']
                        ))

    def drop_changed_udf(self):
        """
        @brief Drop all functions (UDF) that were removed in new version
        """
        for udf in self._udf:
            for item in self._udf[udf]:
                self._run_sql("DROP FUNCTION IF EXISTS {schema}.{udf}({arg})".
                              format(schema=self._schema,
                                     udf=udf,
                                     arg=item['argument']))

    def drop_changed_uda(self):
        """
        @brief Drop all aggregates (UDA) that were removed in new version
        """
        for uda in self._uda:
            for item in self._uda[uda]:
                self._run_sql("DROP AGGREGATE IF EXISTS {schema}.{uda}({arg})".
                              format(schema=self._schema,
                                     uda=uda,
                                     arg=item['argument']))

    def drop_changed_udc(self):
        """
        @brief Drop all casts (UDC) that were updated/removed in new version
        @note We have special treatment for UDCs defined in the svec module
        """
        for udc in self._udc:
            self._run_sql("DROP CAST IF EXISTS ({sourcetype} AS {targettype})".
                          format(sourcetype=self._udc[udc]['sourcetype'],
                                 targettype=self._udc[udc]['targettype']))


class ViewDependency(UpgradeBase):
    """
    @brief This class detects the direct/recursive view dependencies on MADLib
    UDFs/UDAs defined in the current version
    """
    def __init__(self, schema, portid, con_args):
        UpgradeBase.__init__(self, schema, portid, con_args)
        self._view2proc = None
        self._view2view = None
        self._view2def = None
        self._detect_direct_view_dependency()
        self._detect_recursive_view_dependency()
        self._filter_recursive_view_dependency()

    """
    @brief  Detect direct view dependencies on MADLib UDFs/UDAs
    """
    def _detect_direct_view_dependency(self):
        rows = self._run_sql("""
            SELECT
                view, nsp.nspname AS schema, procname, procoid, proisagg
            FROM
                pg_namespace nsp,
                (
                    SELECT
                        c.relname AS view,
                        c.relnamespace AS namespace,
                        p.proname As procname,
                        p.oid AS procoid,
                        p.proisagg AS proisagg
                    FROM
                        pg_class AS c,
                        pg_rewrite AS rw,
                        pg_depend AS d,
                        pg_proc AS p
                    WHERE
                        c.oid = rw.ev_class AND
                        rw.oid = d.objid AND
                        d.classid = 'pg_rewrite'::regclass AND
                        d.refclassid = 'pg_proc'::regclass AND
                        d.refobjid = p.oid AND
                        p.pronamespace = {schema_madlib_oid}
                ) t1
            WHERE
                t1.namespace = nsp.oid
        """.format(schema_madlib_oid=self._schema_oid))

        self._view2proc = defaultdict(list)
        for row in rows:
            key = (row['schema'], row['view'])
            self._view2proc[key].append(
                (row['procname'], row['procoid'],
                    True if row['proisagg'] == 't' else False))

    """
    @brief  Detect recursive view dependencies (view on view)
    """
    def _detect_recursive_view_dependency(self):
        rows = self._run_sql("""
            SELECT
                nsp1.nspname AS depender_schema,
                depender,
                nsp2.nspname AS dependee_schema,
                dependee
            FROM
                pg_namespace AS nsp1,
                pg_namespace AS nsp2,
                (
                    SELECT
                        c.relname depender,
                        c.relnamespace AS depender_nsp,
                        c1.relname AS dependee,
                        c1.relnamespace AS dependee_nsp
                    FROM
                        pg_rewrite AS rw,
                        pg_depend AS d,
                        pg_class AS c,
                        pg_class AS c1
                    WHERE
                        rw.ev_class = c.oid AND
                        rw.oid = d.objid AND
                        d.classid = 'pg_rewrite'::regclass AND
                        d.refclassid = 'pg_class'::regclass AND
                        d.refobjid = c1.oid AND
                        c1.relkind = 'v' AND
                        c.relname <> c1.relname
                    GROUP BY
                        depender, depender_nsp, dependee, dependee_nsp
                ) t1
            WHERE
                t1.depender_nsp = nsp1.oid AND
                t1.dependee_nsp = nsp2.oid
        """)

        self._view2view = defaultdict(list)
        for row in rows:
            key = (row['depender_schema'], row['depender'])
            val = (row['dependee_schema'], row['dependee'])
            self._view2view[key].append(val)

    """
    @brief  Filter out recursive view dependencies which are independent of
    MADLib UDFs/UDAs
    """
    def _filter_recursive_view_dependency(self):
        # Get recursive dependee list
        dependeelist = []
        checklist = self._view2proc
        while True:
            dependeelist.extend(checklist.keys())
            new_checklist = defaultdict(bool)
            for depender in self._view2view.keys():
                for dependee in self._view2view[depender]:
                    if dependee in checklist:
                        new_checklist[depender] = True
                        break
            if len(new_checklist) == 0:
                break
            else:
                checklist = new_checklist

        # Filter recursive dependencies not related with MADLib UDF/UDAs
        fil_view2view = defaultdict(list)
        for depender in self._view2view:
            dependee = self._view2view[depender]
            dependee = [r for r in dependee if r in dependeelist]
            if len(dependee) > 0:
                fil_view2view[depender] = dependee

        self._view2view = fil_view2view

    """
    @brief  Build the dependency graph (depender-to-dependee adjacency list)
    """
    def _build_dependency_graph(self, hasProcDependency=False):
        der2dee = self._view2view.copy()
        for view in self._view2proc:
            if view not in self._view2view:
                der2dee[view] = []
            if hasProcDependency:
                der2dee[view].extend(self._view2proc[view])

        graph = der2dee.copy()
        for der in der2dee:
            for dee in der2dee[der]:
                if dee not in graph:
                    graph[dee] = []
        return graph

    """
    @brief Check dependencies
    """
    def has_dependency(self):
        return len(self._view2proc) > 0

    """
    @brief Get the ordered views for creation
    """
    def get_create_order_views(self):
        graph = self._build_dependency_graph()
        ordered_views = []
        while True:
            remove_list = []
            for depender in graph:
                if len(graph[depender]) == 0:
                    ordered_views.append(depender)
                    remove_list.append(depender)
            for view in remove_list:
                del graph[view]
            for depender in graph:
                graph[depender] = [r for r in graph[depender]
                                   if r not in remove_list]
            if len(remove_list) == 0:
                break
        return ordered_views

    """
    @brief Get the ordered views for dropping
    """
    def get_drop_order_views(self):
        ordered_views = self.get_create_order_views()
        ordered_views.reverse()
        return ordered_views

    """
    @brief Get the depended UDF/UDA signatures for comparison
    """
    def get_depended_func_signature(self, aggregate=True):
        res = {}
        for procs in self._view2proc.values():
            for proc in procs:
                if proc[2] is aggregate and (self._schema, proc) not in res:
                    funcinfo = self._get_function_info(proc[1])
                    signature = get_signature_for_compare(self._schema, proc[0],
                                                          funcinfo['rettype'],
                                                          funcinfo['argument'])
                    res[signature] = True
        return res

    def get_proc_w_dependency(self, aggregate=True):
        res = []
        for procs in self._view2proc.values():
            for proc in procs:
                if proc[2] is aggregate and (self._schema, proc) not in res:
                    res.append((self._schema, proc))
        res.sort()
        return res

    def get_depended_uda(self):
        """
        @brief Get dependent UDAs
        """
        self.get_proc_w_dependency(aggregate=True)

    def get_depended_udf(self):
        """
        @brief Get dependent UDFs
        """
        self.get_proc_w_dependency(aggregate=False)

    def save_and_drop(self):
        """
        @brief Save and drop the dependent views
        """
        self._view2def = {}
        ordered_views = self.get_drop_order_views()
        # Save views
        for view in ordered_views:
            row = self._run_sql("""
                    SELECT
                        schemaname, viewname, viewowner, definition
                    FROM
                        pg_views
                    WHERE
                        schemaname = '{schemaname}' AND
                        viewname = '{viewname}'
                    """.format(schemaname=view[0], viewname=view[1]))
            self._view2def[view] = row[0]

        # Drop views
        for view in ordered_views:
            self._run_sql("""
                DROP VIEW IF EXISTS {schema}.{view}
                """.format(schema=view[0], view=view[1]))

    def restore(self):
        """
        @brief Restore the dependent views
        """
        ordered_views = self.get_create_order_views()
        for view in ordered_views:
            row = self._view2def[view]
            schema = row['schemaname']
            view = row['viewname']
            owner = row['viewowner']
            definition = row['definition']
            self._run_sql("""
                --Alter view not supported by GP, so use set/reset role as a
                --workaround
                --ALTER VIEW {schema}.{view} OWNER TO {owner}
                SET ROLE {owner};
                CREATE OR REPLACE VIEW {schema}.{view} AS {definition};
                RESET ROLE
                """.format(
                    schema=schema, view=view,
                    definition=definition,
                    owner=owner))

    def _node_to_str(self, node):
        if len(node) == 2:
            res = '%s.%s' % (node[0], node[1])
        else:
            res = '%s.%s{oid=%s,isagg=%s}' % (
                self._schema, node[0], node[1], node[2])
        return res

    def _nodes_to_str(self, nodes):
        return [self._node_to_str(i) for i in nodes]

    def get_dependency_graph_str(self):
        """
        @brief Get the dependency graph string for print
        """
        graph = self._build_dependency_graph(True)
        nodes = list(graph.keys())
        nodes.sort()
        res = ["\tDependency Graph (Depender-Dependee Adjacency List):"]
        for node in nodes:
            res.append("{0} -> {1}".format(self._node_to_str(node),
                                           self._nodes_to_str(graph[node])))
        return "\n\t\t\t\t".join(res)


class TableDependency(UpgradeBase):
    """
    @brief This class detects the table dependencies on MADLib UDTs defined in the
    current version
    """
    def __init__(self, schema, portid, con_args):
        UpgradeBase.__init__(self, schema, portid, con_args)
        self._table2type = None
        self._detect_table_dependency()

    def _detect_table_dependency(self):
        """
        @brief Detect the table dependencies on MADLib UDTs
        """
        rows = self._run_sql("""
            SELECT
                nsp.nspname AS schema,
                relname AS relation,
                attname AS column,
                typname AS type
            FROM
                pg_attribute a,
                pg_class c,
                pg_type t,
                pg_namespace nsp
            WHERE
                t.typnamespace = {schema_madlib_oid}
                AND a.atttypid = t.oid
                AND c.oid = a.attrelid
                AND c.relnamespace = nsp.oid
                AND c.relkind = 'r'
            ORDER BY
                nsp.nspname, relname, attname, typname
            """.format(schema_madlib_oid=self._schema_oid))

        self._table2type = defaultdict(list)
        for row in rows:
            key = (row['schema'], row['relation'])
            self._table2type[key].append(
                (row['column'], row['type']))

    def has_dependency(self):
        """
        @brief Check dependencies
        """
        return len(self._table2type) > 0

    def get_depended_udt(self):
        """
        @brief Get the list of depended UDTs
        """
        res = defaultdict(bool)
        for table in self._table2type:
            for (col, typ) in self._table2type[table]:
                if typ not in res:
                    res[typ] = True
        return res

    def get_dependency_str(self):
        """
        @brief Get the dependencies in string for print
        """
        res = ['\tTable Dependency (schema.table.column -> MADlib type):']
        for table in self._table2type:
            for (col, udt) in self._table2type[table]:
                res.append("{0}.{1}.{2} -> {3}".format(table[0], table[1], col,
                                                       udt))
        return "\n\t\t\t\t".join(res)


class ScriptCleaner(UpgradeBase):
    """
    @brief This class removes sql statements from a sql script which should not be
    executed during the upgrade
    """
    def __init__(self, schema, portid, con_args, change_handler):
        UpgradeBase.__init__(self, schema, portid, con_args)
        self._ch = change_handler
        self._sql = None
        self._existing_uda = None
        self._existing_udt = None
        self._aggregate_patterns = self._get_all_aggregate_patterns()
        # print("Number of existing UDAs = " + str(len(self._existing_uda)))
        # print("Number of UDAs to not create = " + str(len(self._aggregate_patterns)))
        self._get_existing_udt()

    def _get_existing_uda(self):
        """
        @brief Get the existing UDAs in the current version
        """
        rows = self._run_sql("""
            SELECT
                max(proname) AS proname,
                max(rettype) AS rettype,
                array_to_string(array_agg(argtype order by i), ',') AS argument
            FROM
            (
                SELECT
                    p.oid AS procoid,
                    proname,
                    textin(regtypeout(prorettype::regtype)) AS rettype,

                    CASE array_upper(proargtypes,1) WHEN -1 THEN ''
                        ELSE textin(regtypeout(unnest(proargtypes)::regtype))
                    END AS argtype,

                    CASE array_upper(proargtypes,1) WHEN -1 THEN 1
                        ELSE generate_series(0, array_upper(proargtypes, 1))
                    END AS i
                FROM
                    pg_proc AS p,
                    pg_namespace AS nsp
                WHERE
                    p.pronamespace = nsp.oid AND
                    p.proisagg = true AND
                    nsp.nspname = '{schema}'
            ) AS f
            GROUP BY
                procoid
            """.format(schema=self._schema))
        self._existing_uda = defaultdict(list)
        for row in rows:
            # Consider about the overloaded aggregates
            self._existing_uda[row['proname']].append(
                                    {'rettype': row['rettype'],
                                     'argument': row['argument']})

    def _get_all_aggregate_patterns(self):
        """
        Creates a list of string patterns that represent all possible
        'CREATE AGGREGATE' statements except ones that are being
        replaced/introduced as part of this upgrade.

        """
        self._get_existing_uda()
        aggregate_patterns = []
        for each_uda, uda_details in self._existing_uda.iteritems():
            for each_item in uda_details:
                if each_uda in self._ch.uda:
                    if each_item in self._ch.uda[each_uda]:
                        continue
                p_arg_str = ''
                argument = each_item['argument']
                args = argument.split(',')
                for arg in args:
                    arg = self._rewrite_type_in(arg.strip())
                    if p_arg_str == '':
                        p_arg_str += '%s\s*' % arg
                    else:
                        p_arg_str += ',\s*%s\s*' % arg
                p_str = "CREATE\s+(ORDERED\s)*\s*AGGREGATE" \
                        "\s+%s\.(%s)\s*\(\s*%s\)(.*?);" % (self._schema.upper(),
                                                           each_uda,
                                                           p_arg_str)
                aggregate_patterns.append(p_str)
        return aggregate_patterns

    def _get_existing_udt(self):
        """
        @brief Get the existing UDTs in the current version
        """
        rows = self._run_sql("""
            SELECT
                typname
            FROM
                pg_type AS t,
                pg_namespace AS nsp
            WHERE
                t.typnamespace = nsp.oid AND
                nsp.nspname = '{schema}'
            """.format(schema=self._schema))
        self._existing_udt = [row['typname'] for row in rows]

    def get_change_handler(self):
        """
        @note The changer_handler is needed for deciding which sql statements to
        remove
        """
        return self._ch

    def _clean_comment(self):
        """
        @brief Remove comments in the sql script
        """
        pattern = re.compile(r"""(/\*(.|[\r\n])*?\*/)|(--(.*|[\r\n]))""")
        res = ''
        lines = re.split(r'[\r\n]+', self._sql)
        for line in lines:
            tmp = line
            if not tmp.strip().startswith("E'"):
                line = re.sub(pattern, '', line)
            res += line + '\n'
        self._sql = res.strip()
        #self._sql = re.sub(pattern, '', self._sql).strip()

    """
    @breif Remove "drop/create type" statements in the sql script
    """
    def _clean_type(self):
        # remove 'drop type'
        pattern = re.compile('DROP(\s+)TYPE(.*?);', re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)

        # remove 'create type'
        udt_str = ''
        for udt in self._existing_udt:
            if udt in self._ch.udt:
                continue
            if udt_str == '':
                udt_str += udt
            else:
                udt_str += '|' + udt
        p_str = 'CREATE(\s+)TYPE(\s+)%s\.(%s)(.*?);' % (self._schema.upper(), udt_str)
        pattern = re.compile(p_str, re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)

    """
    @brief Remove "drop/create cast" statements in the sql script
    """
    def _clean_cast(self):
        # remove 'drop cast'
        pattern = re.compile('DROP(\s+)CAST(.*?);', re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)

        # remove 'create cast'
        udc_str = ''
        for udc in self._ch.udc:
            if udc_str == '':
                udc_str += '%s\s+AS\s+%s' % (
                    self._ch.udc[udc]['sourcetype'],
                    self._ch.udc[udc]['targettype'])
            else:
                udc_str += '|' + '%s\s+AS\s+%s' % (
                    self._ch.udc[udc]['sourcetype'],
                    self._ch.udc[udc]['targettype'])

        pattern = re.compile('CREATE\s+CAST(.*?);', re.DOTALL | re.IGNORECASE)
        if udc_str != '':
            pattern = re.compile('CREATE\s+CAST\s*\(\s*(?!%s)(.*?);' %
                                 udc_str, re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)

    """
    @brief Remove "drop/create operator" statements in the sql script
    """
    def _clean_operator(self):
        # remove 'drop operator'
        pattern = re.compile('DROP(\s+)OPERATOR(.*?);', re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)

        # remove 'create operator'
        pattern = re.compile(r"""CREATE(\s+)OPERATOR(.*?);""", re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)

    """
    @brief Rewrite the type
    """
    def _rewrite_type_in(self, arg):
        type_mapper = {
            'smallint': '(int2|smallint)',
            'integer': '(int|int4|integer)',
            'bigint': '(int8|bigint)',
            'double precision': '(float8|double precision)',
            'real': '(float4|real)',
            'character varying': '(varchar|character varying)'
        }
        for typ in type_mapper:
            arg = arg.replace(typ, type_mapper[typ])
        return arg.replace('[', '\[').replace(']', '\]')

    def _clean_aggregate(self):
        # remove all drop aggregate statements
        self._sql = re.sub(re.compile('DROP(\s+)AGGREGATE(.*?);',
                                      re.DOTALL | re.IGNORECASE),
                           '', self._sql)
        # remove all create aggregate statements except ones that should
        #  be created as part of upgrade
        for each_pattern in self._aggregate_patterns:
            regex_pat = re.compile(each_pattern, re.DOTALL | re.IGNORECASE)
            self._sql = re.sub(regex_pat, '', self._sql)

    def _clean_function(self):
        """
        @brief Remove "drop function" statements and rewrite "create function"
        statements in the sql script
        @note We don't drop any function
        """
        # remove 'drop function'
        pattern = re.compile(r"""DROP(\s+)FUNCTION(.*?);""", re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, '', self._sql)
        # replace 'create function' with 'create or replace function'
        pattern = re.compile(r"""CREATE(\s+)FUNCTION""", re.DOTALL | re.IGNORECASE)
        self._sql = re.sub(pattern, 'CREATE OR REPLACE FUNCTION', self._sql)

    def cleanup(self, sql):
        """
        @brief Entry function for cleaning the sql script
        """
        self._sql = sql
        self._clean_comment()
        self._clean_type()
        self._clean_cast()
        self._clean_operator()
        self._clean_aggregate()
        self._clean_function()
        return self._sql
