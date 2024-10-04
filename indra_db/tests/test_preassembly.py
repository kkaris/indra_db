import os
import json
import pickle
import random
import logging
from datetime import datetime
from time import sleep
import pytest

gm_logger = logging.getLogger('grounding_mapper')
gm_logger.setLevel(logging.WARNING)

sm_logger = logging.getLogger('sitemapper')
sm_logger.setLevel(logging.WARNING)

ps_logger = logging.getLogger('phosphosite')
ps_logger.setLevel(logging.WARNING)

pa_logger = logging.getLogger('preassembler')
pa_logger.setLevel(logging.WARNING)

from indra.statements import Statement, Phosphorylation, Agent, Evidence, \
    stmts_from_json, Inhibition, Activation, Complex, IncreaseAmount
from indra.util.nested_dict import NestedDict
from indra.tools import assemble_corpus as ac

from indra_db import util as db_util
from indra_db import client as db_client
from indra_db.preassembly import preassemble_db as pdb
from indra_db.tests.util import get_pa_loaded_db, get_temp_db
from indra_db.tests.db_building_util import DbBuilder

from nose.plugins.attrib import attr

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_ONTOLOGY = os.path.join(HERE, 'test_resources/test_ontology.pkl')
MAX_NUM_STMTS = 11721
BATCH_SIZE = 2017
STMTS = None


# ==============================================================================
# Support classes and functions
# ==============================================================================


class DistillationTestSet(object):
    """A class used to create a test set for distillation."""
    def __init__(self):
        self.d = NestedDict()
        self.stmts = []
        self.target_sets = []
        self.bettered_sids = set()
        self.links = set()
        return

    def add_stmt_to_target_set(self, stmt):
        # If we don't have any target sets of statements, initialize with the
        # input statements.
        if not self.target_sets:
            self.target_sets.append(({stmt},
                                     {self.stmts.index(stmt)}))
        else:
            # Make a copy and empty the current list.
            old_target_sets = self.target_sets[:]
            self.target_sets.clear()

            # Now for every previous scenario, pick a random possible "good"
            # statement, update the corresponding duplicate trace.
            for stmt_set, dup_set in old_target_sets:
                # Here we consider the possibility that each of the
                # potential valid statements may be chosen, and record that
                # possible alteration to the set of possible histories.
                new_set = stmt_set.copy()
                new_set.add(stmt)
                new_dups = dup_set.copy()
                new_dups.add(self.stmts.index(stmt))
                self.target_sets.append((new_set, new_dups))
        return

    def add_content(self, trid, src, tcid, reader, rv_idx, rid, a, b, ev_num,
                    result_class='ignored', has_link=False):
        # Add the new statements to the over-all list.
        stmt = self.__make_test_statement(a, b, reader, ev_num)
        self.stmts.append(stmt)

        # Populate the provenance for the dict.
        rv = db_util.reader_versions[reader][rv_idx]
        r_dict = self.d[trid][src][tcid][reader][rv][rid]

        # If the evidence variation was specified, the evidence in copies is
        # identical, and they will all have the same hash. Else, the hash is
        # different and the statements need to be iterated over.
        s_hash = stmt.get_hash(shallow=False)
        if r_dict.get(s_hash) is None:
            r_dict[s_hash] = set()
        r_dict[s_hash].add((self.stmts.index(stmt), stmt))

        # If this/these statement/s is intended to be picked up, add it/them to
        # the target sets.
        if result_class == 'inc':
            self.add_stmt_to_target_set(stmt)
        elif result_class == 'bet':
            self.bettered_sids.add(self.stmts.index(stmt))

        # If this statement should have a preexisting link, add it
        if has_link:
            self.links.add(self.stmts.index(stmt))
        return

    @staticmethod
    def __make_test_statement(a, b, source_api, ev_num=None):
        A = Agent(a)
        B = Agent(b)
        ev_text = "Evidence %d for %s phosphorylates %s." % (ev_num, a, b)
        ev_list = [Evidence(text=ev_text, source_api=source_api)]
        stmt = Phosphorylation(A, B, evidence=ev_list)
        return stmt

    @classmethod
    def from_tuples(cls, tuples):
        test_set = cls()
        for tpl in tuples:
            test_set.add_content(*tpl)
        return test_set


def make_raw_statement_set_for_distillation():
    test_tuples = [
        (1, ('pubmed', 'abstract'), 1, 'reach', 0, 1, 'A0', 'B0', 1, 'bet'),
        (1, ('pubmed', 'abstract'), 1, 'reach', 0, 1, 'A1', 'B1', 1, 'bet'),
        (1, ('pubmed', 'abstract'), 1, 'reach', 0, 1, 'A1', 'B1', 2, 'bet'),
        (1, ('pubmed', 'abstract'), 1, 'reach', 1, 2, 'A0', 'B0', 1, 'bet', True),
        (1, ('pubmed', 'abstract'), 1, 'reach', 1, 2, 'A1', 'B1', 2, 'inc'),
        (1, ('pubmed', 'abstract'), 1, 'reach', 1, 2, 'A1', 'B1', 4, 'inc'),
        (1, ('pubmed', 'abstract'), 1, 'sparser', 0, 3, 'A1', 'B1', 1),
        (1, ('pubmed', 'abstract'), 1, 'sparser', 0, 3, 'A1', 'B2', 1, 'bet', True),
        (1, ('pubmed', 'abstract'), 1, 'sparser', 0, 3, 'A1', 'B3', 1, 'inc'),
        (1, ('pmc_oa', 'fulltext'), 2, 'reach', 0, 4, 'A0', 'B0', 1, 'bet'),
        (1, ('pmc_oa', 'fulltext'), 2, 'reach', 1, 5, 'A0', 'B0', 1, 'inc'),
        (1, ('pmc_oa', 'fulltext'), 2, 'reach', 1, 5, 'A1', 'B2', 2, 'inc'),
        (1, ('pmc_oa', 'fulltext'), 2, 'reach', 1, 5, 'A1', 'B1', 1, 'inc'),
        (1, ('pmc_oa', 'fulltext'), 2, 'reach', 1, 5, 'A1', 'B1', 3, 'inc'),
        (1, ('pmc_oa', 'fulltext'), 2, 'reach', 1, 5, 'A1', 'B2', 3, 'inc', True),
        (1, ('pmc_oa', 'fulltext'), 2, 'sparser', 1, 6, 'A1', 'B1', 1, 'inc', True),
        (1, ('pmc_oa', 'fulltext'), 2, 'sparser', 1, 6, 'A1', 'B2', 1, 'inc', True),
        (1, ('pmc_oa', 'fulltext'), 2, 'sparser', 1, 6, 'A3', 'B3', 1, 'inc'),
        (1, ('pmc_oa', 'fulltext'), 2, 'sparser', 1, 6, 'A1', 'B1', 4, 'inc'),
        (2, ('pmc_oa', 'fulltext'), 3, 'reach', 1, 7, 'A4', 'B4', 1, 'inc'),
        (2, ('pmc_oa', 'fulltext'), 3, 'reach', 1, 7, 'A1', 'B1', 1, 'inc'),
        (2, ('manuscripts', 'fulltext'), 4, 'reach', 1, 8, 'A3', 'B3', 1, 'inc'),
        (2, ('manuscripts', 'fulltext'), 4, 'reach', 1, 8, 'A1', 'B1', 1)
        ]
    dts = DistillationTestSet.from_tuples(test_tuples)
    return dts.d, dts.stmts, dts.target_sets, dts.bettered_sids, dts.links


def _str_large_set(s, max_num):
    if len(s) > max_num:
        values = list(s)[:max_num]
        ret_str = '{' + ', '.join([str(v) for v in values]) + ' ...}'
        ret_str += ' [length: %d]' % len(s)
    else:
        ret_str = str(s)
    return ret_str


def _do_old_fashioned_preassembly(stmts):
    grounded_stmts = ac.map_grounding(stmts, use_adeft=True,
                                      gilda_mode='local')
    ms_stmts = ac.map_sequence(grounded_stmts, use_cache=True)
    opa_stmts = ac.run_preassembly(ms_stmts, return_toplevel=False,
                                   ontology=_get_test_ontology())
    return opa_stmts


def _get_opa_input_stmts(db):
    stmt_nd = db_util.get_reading_stmt_dict(db, get_full_stmts=True)
    reading_stmts, _ =\
        db_util.get_filtered_rdg_stmts(stmt_nd, get_full_stmts=True)
    db_stmt_jsons = db_client.get_raw_stmt_jsons(
        [db.RawStatements.reading_id.is_(None)],
        db=db
    )
    db_stmts = stmts_from_json(db_stmt_jsons.values())
    stmts = reading_stmts | set(db_stmts)
    print("Got %d statements for vanilla preassembly." % len(stmts))
    return stmts


def _check_against_opa_stmts(db, raw_stmts, pa_stmts):
    def _compare_list_elements(label, list_func, comp_func, **stmts):
        (stmt_1_name, stmt_1), (stmt_2_name, stmt_2) = list(stmts.items())
        vals_1 = [comp_func(elem) for elem in list_func(stmt_1)]
        vals_2 = []
        for element in list_func(stmt_2):
            val = comp_func(element)
            if val in vals_1:
                vals_1.remove(val)
            else:
                vals_2.append(val)
        if len(vals_1) or len(vals_2):
            print("Found mismatched %s for hash %s:\n\t%s=%s\n\t%s=%s"
                  % (label, stmt_1.get_hash(), stmt_1_name, vals_1, stmt_2_name,
                     vals_2))
            return {'diffs': {stmt_1_name: vals_1, stmt_2_name: vals_2},
                    'stmts': {stmt_1_name: stmt_1, stmt_2_name: stmt_2}}
        return None

    opa_stmts = _do_old_fashioned_preassembly(raw_stmts)

    old_stmt_dict = {s.get_hash(): s for s in opa_stmts}
    new_stmt_dict = {s.get_hash(): s for s in pa_stmts}

    new_hash_set = set(new_stmt_dict.keys())
    old_hash_set = set(old_stmt_dict.keys())
    hash_diffs = {'extra_new': [new_stmt_dict[h]
                                for h in new_hash_set - old_hash_set],
                  'extra_old': [old_stmt_dict[h]
                                for h in old_hash_set - new_hash_set]}
    if hash_diffs['extra_new']:
        elaborate_on_hash_diffs(db, 'new', hash_diffs['extra_new'],
                                old_stmt_dict.keys())
    if hash_diffs['extra_old']:
        elaborate_on_hash_diffs(db, 'old', hash_diffs['extra_old'],
                                new_stmt_dict.keys())
    print(hash_diffs)
    tests = [{'funcs': {'list': lambda s: s.evidence[:],
                        'comp': lambda ev: '%s-%s-%s' % (ev.source_api, ev.pmid,
                                                         ev.text)},
              'label': 'evidence text',
              'results': []},
             {'funcs': {'list': lambda s: s.supports[:],
                        'comp': lambda s: s.get_hash()},
              'label': 'supports matches keys',
              'results': []},
             {'funcs': {'list': lambda s: s.supported_by[:],
                        'comp': lambda s: s.get_hash()},
              'label': 'supported-by matches keys',
              'results': []}]
    comp_hashes = new_hash_set & old_hash_set
    for mk_hash in comp_hashes:
        for test_dict in tests:
            res = _compare_list_elements(test_dict['label'],
                                         test_dict['funcs']['list'],
                                         test_dict['funcs']['comp'],
                                         new_stmt=new_stmt_dict[mk_hash],
                                         old_stmt=old_stmt_dict[mk_hash])
            if res is not None:
                test_dict['results'].append(res)

    def all_tests_passed():
        test_results = [not any(hash_diffs.values())]
        for td in tests:
            test_results.append(len(td['results']) == 0)
        print("%d/%d tests passed." % (sum(test_results), len(test_results)))
        return all(test_results)

    def write_report(num_comps):
        ret_str = "Some tests failed:\n"
        ret_str += ('Found %d/%d extra old stmts and %d/%d extra new stmts.\n'
                    % (len(hash_diffs['extra_old']), len(old_hash_set),
                       len(hash_diffs['extra_new']), len(new_hash_set)))
        for td in tests:
            ret_str += ('Found %d/%d mismatches in %s.\n'
                        % (len(td['results']), num_comps, td['label']))
        return ret_str

    # Now evaluate the results for exceptions
    assert all_tests_passed(), write_report(len(comp_hashes))


def str_imp(o, uuid=None, other_stmt_keys=None):
    if o is None:
        return '~'
    cname = o.__class__.__name__
    if cname == 'TextRef':
        return ('<TextRef: trid: %s, pmid: %s, pmcid: %s>'
                % (o.id, o.pmid, o.pmcid))
    if cname == 'TextContent':
        return ('<TextContent: tcid: %s, trid: %s, src: %s>'
                % (o.id, o.text_ref_id, o.source))
    if cname == 'Reading':
        return ('<Reading: rid: %s, tcid: %s, reader: %s, rv: %s>'
                % (o.id, o.text_content_id, o.reader, o.reader_version))
    if cname == 'RawStatements':
        s = Statement._from_json(json.loads(o.json.decode()))
        s_str = ('<RawStmt: %s sid: %s, db: %s, rdg: %s, uuid: %s, '
                 'type: %s, iv: %s, hash: %s>'
                 % (str(s), o.id, o.db_info_id, o.reading_id,
                    o.uuid[:8] + '...', o.type,
                    o.indra_version[:14] + '...', o.mk_hash))
        if other_stmt_keys and s.get_hash() in other_stmt_keys:
            s_str = '+' + s_str
        if s.uuid == uuid:
            s_str = '*' + s_str
        return s_str


def elaborate_on_hash_diffs(db, lbl, stmt_list, other_stmt_keys):
    print("#"*100)
    print("Elaboration on extra %s statements:" % lbl)
    print("#"*100)
    for s in stmt_list:
        print(s)
        uuid = s.uuid
        print('-'*100)
        print('uuid: %s\nhash: %s\nshallow hash: %s'
              % (s.uuid, s.get_hash(shallow=False), s.get_hash()))
        print('-'*100)
        db_pas = db.select_one(db.PAStatements,
                               db.PAStatements.mk_hash == s.get_hash())
        print('\tPA statement:', db_pas.__dict__ if db_pas else '~')
        print('-'*100)
        db_s = db.select_one(db.RawStatements, db.RawStatements.uuid == s.uuid)
        print('\tRaw statement:', str_imp(db_s, uuid, other_stmt_keys))
        if db_s is None:
            continue
        print('-'*100)
        if db_s.reading_id is None:
            print("Statement was from a database: %s" % db_s.db_info_id)
            continue
        db_r = db.select_one(db.Reading, db.Reading.id == db_s.reading_id)
        print('\tReading:', str_imp(db_r))
        tc = db.select_one(db.TextContent,
                           db.TextContent.id == db_r.text_content_id)
        print('\tText Content:', str_imp(tc))
        tr = db.select_one(db.TextRef, db.TextRef.id == tc.text_ref_id)
        print('\tText ref:', str_imp(tr))
        print('-'*100)
        for tc in db.select_all(db.TextContent,
                                db.TextContent.text_ref_id == tr.id):
            print('\t', str_imp(tc))
            for r in db.select_all(db.Reading,
                                   db.Reading.text_content_id == tc.id):
                print('\t\t', str_imp(r))
                for s in db.select_all(db.RawStatements,
                                       db.RawStatements.reading_id == r.id):
                    print('\t\t\t', str_imp(s, uuid, other_stmt_keys))
        print('='*100)


class RefLoadedDb:
    def __init__(self):
        self.db = get_temp_db(clear=True)

        N = int(10**5)
        S = int(10**8)
        self.fake_pmids_a = {(i, str(random.randint(0, S))) for i in range(N)}
        self.fake_pmids_b = {(int(N/2 + i), str(random.randint(0, S)))
                        for i in range(N)}

        self.expected = {id: pmid for id, pmid in self.fake_pmids_a}
        for id, pmid in self.fake_pmids_b:
            self.expected[id] = pmid

        start = datetime.now()
        self.db.copy('text_ref', self.fake_pmids_a, ('id', 'pmid'))
        print("First load:", datetime.now() - start)

        try:
            self.db.copy('text_ref', self.fake_pmids_b, ('id', 'pmid'))
            assert False, "Vanilla copy succeeded when it should have failed."
        except Exception as e:
            self.db._conn.rollback()
            pass

    def check_result(self):
        refs = self.db.select_all([self.db.TextRef.id, self.db.TextRef.pmid])
        result = {id: pmid for id, pmid in refs}
        assert result.keys() == self.expected.keys()
        passed = True
        for id, pmid in self.expected.items():
            if result[id] != pmid:
                print(id, pmid)
                passed = False
        assert passed, "Result did not match expected."


def _get_test_ontology():
    # Load the test ontology.
    with open(TEST_ONTOLOGY, 'rb') as f:
        test_ontology = pickle.load(f)
    return test_ontology


# ==============================================================================
# Test Database Definitions.
# ==============================================================================


mek = Agent('MEK', db_refs={'FPLX': 'MEK', 'TEXT': 'MEK'})
map2k1 = Agent('MAP2K1', db_refs={'HGNC': '6840', 'TEXT': 'MAP2K1'})
map2k1_mg = Agent('MAP2K1', db_refs={'HGNC': '6840', 'TEXT': 'MEK1/2'})
erk = Agent('ERK', db_refs={'FPLX': 'ERK', 'TEXT': 'mapk'})
mapk1 = Agent('MAPK1', db_refs={'HGNC': '6871', 'TEXT': 'mapk1'})
raf = Agent('RAF', db_refs={'FPLX': 'RAF', 'TEXT': 'raf'})
braf = Agent('BRAF', db_refs={'HGNC': '1097', 'TEXT': 'BRAF'})
ras = Agent('RAS', db_refs={'FPLX': 'RAS', 'TEXT': 'RAS'})
kras = Agent('KRAS', db_refs={'HGNC': '6407', 'TEXT': 'KRAS'})
simvastatin = Agent('simvastatin',
                    db_refs={'CHEBI': 'CHEBI:9150', 'TEXT': 'simvastatin'})
simvastatin_ng = Agent('simvastatin', db_refs={'TEXT': 'simvastatin'})


def _get_db_no_pa_stmts():
    db = get_temp_db(clear=True)

    db_builder = DbBuilder(db)
    db_builder.add_text_refs([
        ('12345', 'PMC54321'),
        ('24680', 'PMC08642'),
        ('97531',)
    ])
    db_builder.add_text_content([
        ['pubmed-ttl', 'pubmed-abs', 'pmc_oa'],
        ['pubmed-abs', 'manuscripts'],
        ['pubmed-ttl', 'pubmed-abs']
    ])
    db_builder.add_readings([
        ['REACH', 'TRIPS'],
        ['REACH', 'SPARSER'],
        ['REACH', 'ISI'],
        ['SPARSER'],
        ['REACH', 'SPARSER'],
        ['SPARSER', 'TRIPS', 'REACH'],
        ['REACH', 'EIDOS']
    ])
    db_builder.add_raw_reading_statements([
        [Phosphorylation(mek, erk)],  # reach pubmed title
        [Phosphorylation(mek, erk, 'T', '124')],  # trips pubmed title
        [Phosphorylation(mek, erk), Inhibition(erk, ras),
         (Phosphorylation(mek, erk), 'in the body')],  # reach pubmed-abs
        [Complex([mek, erk]), Complex([erk, ras]),
         (Phosphorylation(None, erk), 'In the body')],  # sparser pubmed-abs
        [],  # reach pmc_oa
        [],  # ISI pmc_oa
        [Phosphorylation(map2k1, mapk1)],  # sparser pubmed-abs
        [],  # reach manuscripts
        [],  # sparser manuscripts
        [Inhibition(simvastatin_ng, raf),
         Activation(map2k1_mg, erk)],  # sparser pubmed title
        [],  # TRIPS pubmed title
        [],  # reach pubmed title
        [],  # reach pubmed abs
        [],  # eidos pubmed abs
    ])
    db_builder.add_databases(['biopax', 'tas', 'bel'])
    db_builder.add_raw_database_statements([
        [Activation(mek, raf), Inhibition(erk, ras), Phosphorylation(mek, erk)],
        [Inhibition(simvastatin, raf)],
        [Phosphorylation(mek, erk, 'T', '124')]
    ])
    return db


def _get_db_with_pa_stmts():
    db = get_temp_db(clear=True)

    db_builder = DbBuilder(db)
    db_builder.add_text_refs([
        ('12345', 'PMC54321'),
        ('24680', 'PMC08642'),
        ('97531',),
        ('87687',)
    ])
    db_builder.add_text_content([
        ['pubmed-ttl', 'pubmed-abs', 'pmc_oa'],
        ['pubmed-ttl', 'pubmed-abs', 'manuscripts'],
        ['pubmed-ttl', 'pubmed-abs', 'pmc_oa'],
        ['pubmed-ttl', 'pubmed-abs']
    ])
    db_builder.add_readings([
        # Ref 1
        ['REACH', 'TRIPS'],  # pubmed ttl
        ['REACH', 'SPARSER'],  # pubmed abs
        ['REACH', 'ISI'],  # pmc_oa
        # Ref 2
        ['REACH', 'TRIPS'],  # pubmed ttl (new)
        ['SPARSER'],  # pubmed abs
        ['REACH', 'SPARSER'],  # manuscripts
        # Ref 3
        ['SPARSER', 'TRIPS', 'REACH'],  # pubmed ttl
        ['REACH', 'EIDOS', 'SPARSER'],  # pubmed abs
        ['SPARSER'],  # pmc oa (new)
        # Ref 4
        ['TRIPS', 'REACH', 'SPARSER'],  # pubmed ttl (new)
        ['REACH', 'SPARSER'],  # pubmed abs (new)
    ])

    db_builder.add_raw_reading_statements([
        # Ref 1
        # pubmed ttl
        [Phosphorylation(mek, erk)],  # reach
        [Phosphorylation(mek, erk, 'T', '124')],  # trips
        # pubmed abs
        [Phosphorylation(mek, erk),
         Inhibition(erk, ras),
         (Phosphorylation(mek, erk), 'in the body')],  # reach
        [Complex([mek, erk]),
         Complex([erk, ras]),
         (Phosphorylation(None, erk), 'In the body')],  # sparser
        # pmc OA
        [],  # reach
        [],  # ISI

        # Ref 2
        # pubmed ttl
        [Phosphorylation(map2k1, mapk1)],  # reach (new)
        [Phosphorylation(map2k1, mapk1, 'T', '124')],  # trips (new)
        # pubmed abs
        [Phosphorylation(map2k1, mapk1)],  # sparser
        # manuscript
        [],  # reach
        [],  # sparser

        # Ref 3
        # pubmed ttl
        [],  # sparser
        [Inhibition(simvastatin, raf)],  # TRIPS
        [],  # reach
        # pubmed abs
        [],  # reach
        [],  # eidos
        [Activation(map2k1_mg, erk),
         Inhibition(simvastatin_ng, raf)],  # sparser (new)
        # pmc oa
        [Inhibition(simvastatin_ng, raf),
         Inhibition(erk, ras),
         Activation(ras, raf)],  # sparser (new)

        # Ref 4
        # pubmed ttl
        [],  # trips (new)
        [],  # reach (new)
        [],  # sparser (new)
        # pubmed abstract
        [Activation(kras, braf),
         Complex([map2k1, mapk1]),
         Complex([kras, braf])],  # reach (new)
        [Complex([kras, braf]),
         Complex([mek, erk]),
         IncreaseAmount(kras, braf)],  # sparser (new)
    ])
    db_builder.add_databases(['biopax', 'tas', 'bel'])
    db_builder.add_raw_database_statements([
        [Activation(mek, raf),
         Inhibition(erk, ras),
         Phosphorylation(mek, erk),
         Activation(ras, raf)],
        [Inhibition(simvastatin, raf)],
        [Phosphorylation(mek, erk, 'T', '124')]
    ])
    db_builder.add_pa_statements([
        (Phosphorylation(mek, erk), [0, 2, 4, 25], [1, 8]),
        (Phosphorylation(mek, erk, 'T', '124'), [1, 28]),
        (Phosphorylation(None, erk), [7], [0, 1, 8]),
        (Activation(mek, raf), [23]),
        (Inhibition(simvastatin, raf), [11, 27]),
        (Complex([mek, erk]), [5]),
        (Complex([erk, ras]), [6]),
        (Inhibition(erk, ras), [3, 24]),
        (Phosphorylation(map2k1, mapk1), [10])
    ])

    # Add the preassembly update.
    pu = db.PreassemblyUpdates(corpus_init=True)
    db.session.add(pu)
    db.session.commit()

    return db


# ==============================================================================
# Tests
# ==============================================================================


def test_distillation_on_curated_set():
    stmt_dict, stmt_list, target_sets, target_bettered_ids, ev_link_sids = \
        make_raw_statement_set_for_distillation()
    filtered_set, bettered_ids = \
        db_util.get_filtered_rdg_stmts(stmt_dict, get_full_stmts=True,
                                       linked_sids=ev_link_sids)
    for stmt_set, dup_set in target_sets:
        if stmt_set == filtered_set:
            break
    else:
        assert False, "Filtered set does not match any valid possibilities."
    assert bettered_ids == target_bettered_ids
    # assert dup_set == duplicate_ids, (dup_set - duplicate_ids,
    #                                   duplicate_ids - dup_set)
    stmt_dict, stmt_list, target_sets, target_bettered_ids, ev_link_sids = \
        make_raw_statement_set_for_distillation()
    filtered_id_set, bettered_ids = \
        db_util.get_filtered_rdg_stmts(stmt_dict, get_full_stmts=False,
                                       linked_sids=ev_link_sids)
    assert len(filtered_id_set) == len(filtered_set), \
        (len(filtered_set), len(filtered_id_set))


@pytest.mark.nonpublic
def test_db_lazy_insert():
    rldb = RefLoadedDb()

    # Try adding more text refs lazily. Overlap is guaranteed.
    start = datetime.now()
    rldb.db.copy_lazy('text_ref', rldb.fake_pmids_b, ('id', 'pmid'))
    print("Lazy copy:", datetime.now() - start)

    rldb.check_result()

    # As a benchmark, see how long this takes the "old fashioned" way.
    rldb.db._clear(force=True)
    start = datetime.now()
    rldb.db.copy('text_ref', rldb.fake_pmids_a, ('id', 'pmid'))
    print('Second load:', datetime.now() - start)

    start = datetime.now()
    current_ids = {trid for trid, in rldb.db.select_all(rldb.db.TextRef.id)}
    clean_fake_pmids_b = {t for t in rldb.fake_pmids_b
                          if t[0] not in current_ids}
    rldb.db.copy('text_ref', clean_fake_pmids_b, ('id', 'pmid'))
    print('Old fashioned copy:', datetime.now() - start)
    return


@pytest.mark.nonpublic
def test_lazy_copier_unique_constraints():
    db = get_temp_db(clear=True)

    N = int(10**5)
    S = int(10**8)
    fake_mids_a = {('man-' + str(random.randint(0, S)),) for _ in range(N)}
    fake_mids_b = {('man-' + str(random.randint(0, S)),) for _ in range(N)}

    assert len(fake_mids_a | fake_mids_b) < len(fake_mids_a) + len(fake_mids_b)

    start = datetime.now()
    db.copy('text_ref', fake_mids_a, ('manuscript_id',))
    print("First load:", datetime.now() - start)

    try:
        db.copy('text_ref', fake_mids_b, ('manuscript_id',))
        assert False, "Vanilla copy succeeded when it should have failed."
    except Exception as e:
        db._conn.rollback()
        pass

    start = datetime.now()
    db.copy_lazy('text_ref', fake_mids_b, ('manuscript_id',))
    print("Lazy copy:", datetime.now() - start)

    mid_results = [mid for mid, in db.select_all(db.TextRef.manuscript_id)]
    assert len(mid_results) == len(set(mid_results)), \
        (len(mid_results), len(set(mid_results)))

    return


@pytest.mark.nonpublic
def test_lazy_copier_update():
    rldb = RefLoadedDb()

    # Try adding more text refs lazily. Overlap is guaranteed.
    start = datetime.now()
    rldb.db.copy_push('text_ref', rldb.fake_pmids_b, ('id', 'pmid'))
    print("Lazy copy:", datetime.now() - start)
    
    rldb.check_result()


@pytest.mark.nonpublic
def test_db_preassembly():
    db = _get_db_no_pa_stmts()

    # Now test the set of preassembled (pa) statements from the database
    # against what we get from old-fashioned preassembly (opa).
    opa_inp_stmts = _get_opa_input_stmts(db)

    # Get the set of raw statements.
    raw_stmt_list = db.select_all(db.RawStatements)
    all_raw_ids = {raw_stmt.id for raw_stmt in raw_stmt_list}
    assert len(raw_stmt_list)

    # Run the preassembly initialization.
    preassembler = pdb.DbPreassembler(batch_size=3, print_logs=True,
                                      ontology=_get_test_ontology())
    preassembler.create_corpus(db)

    # Make sure the number of pa statements is within reasonable bounds.
    pa_stmt_list = db.select_all(db.PAStatements)
    assert 0 < len(pa_stmt_list) < len(raw_stmt_list)

    # Check the evidence links.
    raw_unique_link_list = db.select_all(db.RawUniqueLinks)
    assert len(raw_unique_link_list)
    all_link_ids = {ru.raw_stmt_id for ru in raw_unique_link_list}
    all_link_mk_hashes = {ru.pa_stmt_mk_hash for ru in raw_unique_link_list}
    assert len(all_link_ids - all_raw_ids) is 0
    assert all([pa_stmt.mk_hash in all_link_mk_hashes
                for pa_stmt in pa_stmt_list])

    # Check the support links.
    sup_links = db.select_all([db.PASupportLinks.supporting_mk_hash,
                               db.PASupportLinks.supported_mk_hash])
    assert sup_links
    assert not any([l[0] == l[1] for l in sup_links]), \
        "Found self-support in the database."

    # Try to get all the preassembled statements from the table.
    pa_jsons = db_client.get_pa_stmt_jsons(db=db)
    pa_stmts = stmts_from_json([r['stmt'] for r in pa_jsons.values()])
    assert len(pa_stmts) == len(pa_stmt_list), (len(pa_stmts),
                                                len(pa_stmt_list))

    self_supports = {
        s.get_hash(): s.get_hash() in {s_.get_hash()
                                       for s_ in s.supported_by + s.supports}
        for s in pa_stmts
    }
    if any(self_supports.values()):
        assert False, "Found self-support in constructed pa statement objects."

    _check_against_opa_stmts(db, opa_inp_stmts, pa_stmts)
    return


@pytest.mark.nonpublic
def test_db_preassembly_update():
    db = _get_db_with_pa_stmts()

    # Run the preassembly test.
    preassembler = pdb.DbPreassembler(batch_size=3, print_logs=True,
                                      ontology=_get_test_ontology())
    opa_inp_stmts = _get_opa_input_stmts(db)
    sleep(0.5)
    preassembler.supplement_corpus(db)

    pa_jsons = db_client.get_pa_stmt_jsons(db=db)
    pa_stmts = stmts_from_json([r['stmt'] for r in pa_jsons.values()])
    _check_against_opa_stmts(db, opa_inp_stmts, pa_stmts)
    return


def test_preassembly_create_corpus_div_by_type():
    db = _get_db_no_pa_stmts()
    opa_inp_stmts = _get_opa_input_stmts(db)

    all_types = {t for t, in db.select_all(db.RawStatements.type)}
    for stmt_type in all_types:
        pa = pdb.DbPreassembler(batch_size=2, print_logs=True,
                                ontology=_get_test_ontology(),
                                stmt_type=stmt_type)
        pa.create_corpus(db)

    pa_jsons = db_client.get_pa_stmt_jsons(db=db)
    pa_stmts = stmts_from_json([r['stmt'] for r in pa_jsons.values()])
    _check_against_opa_stmts(db, opa_inp_stmts, pa_stmts)


def test_preassembly_supplement_corpus_div_by_type():
    db = _get_db_with_pa_stmts()
    opa_inp_stmts = _get_opa_input_stmts(db)

    sleep(0.5)

    all_types = {t for t, in db.select_all(db.RawStatements.type)}
    for stmt_type in all_types:
        pa = pdb.DbPreassembler(batch_size=2, print_logs=True,
                                ontology=_get_test_ontology(),
                                stmt_type=stmt_type)
        pa.supplement_corpus(db)

    pa_jsons = db_client.get_pa_stmt_jsons(db=db)
    pa_stmts = stmts_from_json([r['stmt'] for r in pa_jsons.values()])
    _check_against_opa_stmts(db, opa_inp_stmts, pa_stmts)
