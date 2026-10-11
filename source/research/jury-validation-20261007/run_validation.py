"""Offline technical evidence only. No human ability/calibration or production changes.

No questions, answer keys, raw answers or hidden fixtures are written to reports.
All generated SQL runs against domain grader's disposable in-memory SQLite.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import sqlite3
import socket
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
sys.dont_write_bytecode=True
BASE=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'fsp-match/backend'))

def deny_network(*args,**kwargs):
    raise RuntimeError('Offline validation forbids network access')
socket.create_connection=deny_network
socket.socket.connect=deny_network
socket.socket.connect_ex=deny_network
from app.domain.assessment import VERSION, SPECIALIZATIONS, GRADES, build_form, grade_form, public_form, _rows
from app.domain.validation import evaluate_fixture

VARIANTS=20
POLICIES=('weak','borderline','strong','below_middle_cutoff','above_middle_cutoff','competency_blindspot')
PRIMARY=('weak','borderline','strong')

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def filehash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

FILES={name:BASE/f'fsp-match/backend/app/domain/{name}.py' for name in ('assessment','matching','validation')}
source_before={name:{'path':str(p.relative_to(BASE)),'sha256':filehash(p)} for name,p in FILES.items()}
started=time.monotonic()
checks=[]

def check(name,passed,**facts):
    checks.append({'check':name,'passed':bool(passed),**facts})

def refs(form):
    return {key:copy.deepcopy(value['expected']) for key,value in form['_oracle'].items()}

def wrong(q,form):
    expected=form['_oracle'][q['id']]['expected']
    if q['kind']=='choice':
        return next(o['id'] for o in q['options'] if o['id']!=expected)
    if q['kind']=='multi_choice': return []
    if q['kind']=='trace': return []
    if q['kind']=='sql': return 'SELECT 1'
    raise ValueError('unknown kind')

def answers_for(form,policy):
    answers=refs(form)
    if policy=='weak':
        return {q['id']:wrong(q,form) for q in form['questions']}
    if policy=='strong':return answers
    if policy=='competency_blindspot':
        c=Counter(q['competency'] for q in form['questions'])
        q=next(q for q in form['questions'] if c[q['competency']]==1)
        answers[q['id']]=wrong(q,form)
        return answers
    first_trace=next(q for q in form['questions'] if q['kind']=='trace')
    answers[first_trace['id']]=wrong(first_trace,form)
    if policy in ('borderline','below_middle_cutoff'):
        q=next(q for q in form['questions'] if q['kind']=='multi_choice')
        correct=form['_oracle'][q['id']]['expected']
        if len(correct)!=3:raise ValueError('Policy assumes documented three-correct-option blueprint; update controlled expectation explicitly')
        answers[q['id']]=copy.deepcopy(correct[:1 if policy=='borderline' else 2])
    return answers

def expected(policy,grade):
    score={'weak':0,'borderline':66.67,'strong':100,'below_middle_cutoff':73.33,'above_middle_cutoff':80,'competency_blindspot':80}[policy]
    passed=policy in {'strong','above_middle_cutoff'} or policy in {'borderline','below_middle_cutoff'} and grade=='junior'
    return score,passed

def forbidden_keys(value):
    found=[]
    markers=('expected','answer','oracle','fixture','checker','solution','reference_sql','hidden','private')
    if isinstance(value,dict):
        for key,child in value.items():
            if key.startswith('_') or any(m in key.lower() for m in markers):found.append(key)
            found.extend(forbidden_keys(child))
    elif isinstance(value,list):
        for child in value:found.extend(forbidden_keys(child))
    return found

def stats(values):
    return {'n':len(values),'mean':round(statistics.mean(values),4),'min':min(values),'max':max(values),'population_stddev':round(statistics.pstdev(values),6)}

def independently_aggregate_sql(form):
    q=next(q for q in form['questions'] if q['kind']=='sql')
    oracle=form['_oracle'][q['id']]
    for fixture in oracle['fixtures']:
        sums={}
        if form['grade']=='junior':
            for _,customer,amount,status in fixture['tables']['sales']['rows']:
                if status=='paid':
                    value=sums.setdefault(customer,None)
                    if amount is not None:sums[customer]=(value or 0)+amount
            python_rows=sorted([[c,v] for c,v in sums.items() if v is not None and v>=fixture['threshold']])
        else:
            has_items={row[1] for row in fixture['tables']['items']['rows']}
            for oid,customer,amount in fixture['tables']['orders']['rows']:
                if oid in has_items:
                    count,total=sums.get(customer,(0,0));sums[customer]=(count+1,total+amount)
            python_rows=sorted([[c,count,total] for c,(count,total) in sums.items()])
        if sorted(_rows(oracle['expected'],fixture))!=python_rows:return False
    return True

category_results=[];individual=[];copies=[];sql_fixture_checks=0
for spec in SPECIALIZATIONS:
    for grade in GRADES:
        forms=[build_form(spec,grade,f'jury-offline-20261007/{spec}/{grade}/variant-{i:02d}') for i in range(VARIANTS)]
        category=f'{spec}/{grade}'
        check(category+'/documented_rubric',all(f['rubric']['pass_score']==(65 if grade=='junior' else 75) and f['rubric']['minimum_competency_percentage']==50 for f in forms))
        reproducible=all(build_form(spec,grade,f'jury-offline-20261007/{spec}/{grade}/variant-{i:02d}')==forms[i] for i in range(VARIANTS))
        check(category+'/same_seed_reproduces_full_form',reproducible,cases=VARIANTS)
        ids=[f['form_id'] for f in forms];check(category+'/distinct_form_ids',len(set(ids))==VARIANTS,cases=VARIANTS)
        blueprints={digest(f['blueprint']) for f in forms}
        distributions={digest(sorted((q['kind'],q['competency'],q['max_points']) for q in f['questions'])) for f in forms}
        check(category+'/same_blueprint',len(blueprints)==1,distinct_blueprints=len(blueprints))
        check(category+'/same_kind_competency_weight_distribution',len(distributions)==1,distinct_distributions=len(distributions))
        check(category+'/five_items_100_points',all(len(f['questions'])==5 and sum(q['max_points'] for q in f['questions'])==100 for f in forms))
        public=[public_form(f) for f in forms]
        leaks=[forbidden_keys(p) for p in public]
        check(category+'/public_projection_excludes_answer_hidden_fields',all(not leak for leak in leaks),cases=VARIANTS)
        content_hashes=[]
        for p in public:
            without_identity=copy.deepcopy(p);without_identity.pop('form_id');content_hashes.append(digest(without_identity))
        check(category+'/content_diversity_excludes_identity',len(set(content_hashes))>1,unique_content=len(set(content_hashes)),cases=VARIANTS)
        if spec=='data_sql':
            ok=all(independently_aggregate_sql(f) for f in forms);sql_fixture_checks+=len(forms)*3
            check(category+'/sql_oracle_matches_independent_python_aggregation',ok,fixture_count=len(forms)*3)
        outcomes={p:[] for p in POLICIES}
        repeats_ok=True;expected_ok=True;order_ok=True;competencies_ok=True
        for i,form in enumerate(forms):
            current={}
            for policy in POLICIES:
                answers=answers_for(form,policy)
                r=grade_form(form,answers);repeat=grade_form(form,copy.deepcopy(answers))
                repeats_ok &= r==repeat
                want_score,want_pass=expected(policy,grade)
                expected_ok &= r['score']==want_score and r['passed']==want_pass
                min_comp=min(c['percentage'] for c in r['competencies'].values())
                if policy in ('borderline','below_middle_cutoff','above_middle_cutoff','strong'):competencies_ok &= min_comp>=50
                row={'category':category,'variant_index':i,'policy':policy,'score':r['score'],'passed':r['passed'],'confirmed_outcome':grade if r['passed'] else 'unconfirmed','minimum_competency_percentage':min_comp,'expected_score':want_score,'expected_passed':want_pass}
                individual.append(row);outcomes[policy].append(row);current[policy]=row
            order_ok &= current['weak']['score']<current['borderline']['score']<current['strong']['score']
        check(category+'/repeat_grading_same_response',repeats_ok,cases=VARIANTS*len(POLICIES))
        check(category+'/controlled_expected_score_and_confirmation',expected_ok,cases=VARIANTS*len(POLICIES))
        check(category+'/technical_weak_borderline_strong_order',order_ok,cases=VARIANTS)
        check(category+'/boundary_profiles_minimum_competency_satisfied',competencies_ok,cases=VARIANTS*4)
        profile_metrics={}
        for policy,rows in outcomes.items():
            pass_labels=[r['passed'] for r in rows];scores=[r['score'] for r in rows]
            pairs=[(rows[i],rows[i+1]) for i in range(0,VARIANTS,2)]
            differences=[b['score']-a['score'] for a,b in pairs]
            profile_metrics[policy]={'score':stats(scores),'pass_count':sum(pass_labels),'pass_rate':round(sum(pass_labels)/len(pass_labels),4),'expected_confirmed_outcome':grade if expected(policy,grade)[1] else 'unconfirmed','test_retest':{'paired_alternate_forms':len(pairs),'confirmation_agreement':sum(a['confirmed_outcome']==b['confirmed_outcome'] for a,b in pairs)/len(pairs),'score_difference':stats(differences),'mean_absolute_score_difference':statistics.mean(abs(v) for v in differences),'score_correlation':None,'cohen_kappa':None,'undefined_reason':'Scores and outcomes are constant within each oracle-controlled policy; correlations/kappa are degenerate, not evidence of psychometric reliability.'}}
        donor=refs(forms[0]);copy_rows=[]
        for i,form in enumerate(forms[1:],1):
            r=grade_form(form,copy.deepcopy(donor));row={'category':category,'donor_variant_index':0,'target_variant_index':i,'score':r['score'],'passed':r['passed']};copies.append(row);copy_rows.append(row)
        copy_metric={'trials':len(copy_rows),'score':stats([r['score'] for r in copy_rows]),'pass_count':sum(r['passed'] for r in copy_rows),'pass_rate':round(sum(r['passed'] for r in copy_rows)/len(copy_rows),6)}
        distribution=Counter((q['kind'],q['competency'],q['max_points']) for q in forms[0]['questions'])
        category_results.append({'category':category,'variants':VARIANTS,'threshold':forms[0]['rubric']['pass_score'],'minimum_competency_percentage':50,'blueprint_unique_count':len(blueprints),'content_unique_count_excluding_form_id':len(set(content_hashes)),'distribution':[{'kind':kind,'competency':comp,'max_points':points,'items':count} for (kind,comp,points),count in sorted(distribution.items())],'controlled_profiles':profile_metrics,'literal_copy_experiment':copy_metric})

fixture_path=BASE/'fsp-match/validation/benchmarks/matching-v1.json'
fixture_bytes=fixture_path.read_bytes();fixture=json.loads(fixture_bytes)
matching=evaluate_fixture(fixture)
old=json.loads((BASE/'fsp-match/validation/benchmarks/matching-results-v1.json').read_text())
comparison={'same_fixture_canonical_sha256':matching['fixture_sha256']==old['fixture_sha256'],'same_matching_version':matching['matching_version']==old['matching_version'],'same_all_modes_metrics':matching['modes']==old['modes']}
check('matching/frozen_fixture_not_modified',fixture_path.read_bytes()==fixture_bytes)
check('matching/gold_covers_all_pairs',matching['labeled_pairs']==480)
check('matching/current_metrics_equal_previous_reference',all(comparison.values()),comparison=comparison)
source_after={name:filehash(p) for name,p in FILES.items()}
check('source_hashes_stable_during_run',all(source_before[n]['sha256']==source_after[n] for n in source_before))

report={'schema_version':'jury-technical-validation-20261007-v1','created_at_utc':datetime.now(timezone.utc).isoformat(),'status':'TECHNICAL_SYNTHETIC_EVIDENCE_NOT_INDEPENDENT_HUMAN_VALIDATION','assessment_version':VERSION,'runtime':{'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version},'validation_script_sha256':filehash(Path(__file__)),'source_binding':source_before,'release_binding':'SOURCE_HASHES_ONLY_ROOT_BINDS_FINAL_RELEASE','offline':True,'network_guard_enabled':True,'production_mutations':False,'llm_used':False,'real_accounts_used':False,'question_or_answer_keys_written':False,'source_unchanged_during_run':all(source_before[n]['sha256']==source_after[n] for n in source_before),'counts':{'categories':len(category_results),'forms':len(category_results)*VARIANTS,'unique_primary_profile_evaluations':len(category_results)*VARIANTS*len(PRIMARY),'all_controlled_policy_evaluations':len(individual),'repeat_grading_evaluations':len(individual),'paired_primary_alternate_form_comparisons':len(category_results)*(VARIANTS//2)*len(PRIMARY),'literal_copied_answer_trials':len(copies),'independent_python_sql_fixture_checks':sql_fixture_checks,'failed_checks':sum(not c['passed'] for c in checks),'checks':len(checks)},'policy_definitions':{'weak':'Deliberately wrong responses to each item: expected score0, no confirmation.','borderline':'Correct all except first trace; select one of three correct multi-choice options: score66.67, Junior confirmation, Middle unconfirmed.','strong':'Trusted reference correct responses: score100, confirmation for requested grade.','below_middle_cutoff':'First trace incorrect and two of three correct multi-choice: score73.33; Junior passes, Middle does not.','above_middle_cutoff':'First trace incorrect, other answers correct: score80; all competency floors met, requested grade confirmed.','competency_blindspot':'One single-item competency deliberately incorrect, rest correct: score80 but competency floor blocks confirmation.'},'categories':category_results,'individual_scores':individual,'literal_copy_scores':copies,'matching':{'fixture_relative_path':str(fixture_path.relative_to(BASE)),'fixture_file_sha256':hashlib.sha256(fixture_bytes).hexdigest(),'fixture_canonical_sha256':matching['fixture_sha256'],'version':fixture['version'],'profiles':matching['candidate_count'],'needs':matching['need_count'],'labeled_pairs':matching['labeled_pairs'],'split_needs':dict(Counter(n['split'] for n in fixture['needs'])),'provenance':matching['gold_provenance'],'external_blind_holdout':False,'human_validation':matching['human_validation'],'comparison_to_previous':comparison,'modes':matching['modes']},'checks':checks,'limitations':['Controlled correct/incorrect responses are constructed with trusted domain oracle; test-retest agreement proves deterministic checker behavior for these response patterns, not human competence stability or psychometric form equivalence.','No independent human benchmark, IRT calibration, empirical item discrimination, statistical population sample, user UX, API cooldown, email, bot or production workflow is validated here.','Twenty seeded variants per category share a bounded task family; changing parameters/option labels does not create twenty independent skill domains.','Literal-copy pass counts are retained honestly. Semantic strategies, SQL template knowledge, LLM assistance and entire-family leaks can transfer between forms; no anti-cheating guarantee.','Matching labels are team-authored synthetic business judgments, claimed frozen before evaluation in existing fixture metadata; this run does not independently establish historical creation order or external expert independence.','One holdout query has no positive gold; nDCG excludes it, whereas Precision uses all six queries. Fixed Precision@k denominator includes unfilled positions as zero.','No live candidate qualification or account state is changed.'],'elapsed_seconds':round(time.monotonic()-started,3)}
(OUT/'validation-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':report['status'],'counts':report['counts'],'elapsed_seconds':report['elapsed_seconds'],'copy_passes':sum(r['passed'] for r in copies),'copy_means':{c['category']:c['literal_copy_experiment']['score']['mean'] for c in category_results},'holdout_evidence':{k:v for k,v in matching['modes']['evidence']['holdout']['at_5'].items() if k!='queries'},'source_hashes':{k:v['sha256'] for k,v in source_before.items()}},ensure_ascii=False,indent=2))
if any(not c['passed'] for c in checks):raise SystemExit(1)
