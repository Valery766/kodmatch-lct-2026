import test from 'node:test';
import assert from 'node:assert/strict';
import { blockedPageNotice, resolvePage, sessionIdentity } from '../src/navigation.ts';
import { readAssessmentDraft, writeAssessmentDraft, removeAssessmentDraft } from '../src/assessmentDraft.ts';

test('an employer opening a stale candidate test link lands in their home', () => {
  assert.equal(resolvePage('assessment', 'employer'), 'home');
  assert.match(blockedPageNotice('assessment', 'employer'), /аккаунт работодателя/);
  assert.match(blockedPageNotice('assessment', 'employer'), /войдите как кандидат/);
});

test('a candidate can open the real test link directly', () => {
  assert.equal(resolvePage('assessment', 'candidate'), 'assessment');
  assert.equal(blockedPageNotice('assessment', 'candidate'), '');
});

test('all private candidate pages are blocked to an employer', () => {
  for (const page of ['assessment', 'profile']) assert.equal(resolvePage(page, 'employer'), 'home');
});

test('all private employer pages are blocked to a candidate', () => {
  for (const page of ['company', 'search', 'shortlist']) assert.equal(resolvePage(page, 'candidate'), 'home');
  assert.match(blockedPageNotice('search', 'candidate'), /аккаунт кандидата/);
});

test('common pages survive account-role changes and browser back navigation', () => {
  for (const page of ['home', 'invitations', 'vacancies', 'responses', 'microtasks', 'history', 'settings', 'notifications']) {
    for (const role of ['candidate', 'employer']) assert.equal(resolvePage(page, role), page);
  }
});

test('unknown landing anchors and malformed hashes do not mount private pages', () => {
  for (const page of ['', 'how', 'principles', 'assessment?role=candidate', '../assessment', '__proto__']) {
    for (const role of ['candidate', 'employer']) assert.equal(resolvePage(page, role), 'home');
  }
});

test('different users of the same role and logout have distinct identities', () => {
  assert.notEqual(sessionIdentity({id:'first',role:'candidate'}), sessionIdentity({id:'second',role:'candidate'}));
  assert.notEqual(sessionIdentity({id:'first',role:'candidate'}), sessionIdentity({id:'first',role:'employer'}));
  assert.equal(sessionIdentity(null), '');
});

function storage() {
  const values=new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value), removeItem: key => values.delete(key) };
}

test('answers survive navigation or refresh of the same active test', () => {
  const saved=storage();const answers={q1:'o2',q2:['o1','o3'],q3:'SELECT id FROM users'};
  writeAssessmentDraft(saved,'candidate-a','attempt-a',5000,answers);
  assert.deepEqual(readAssessmentDraft(saved,'candidate-a','attempt-a',5000,4000),answers);
});

test('logging into another candidate account never loads a previous user draft', () => {
  const saved=storage();writeAssessmentDraft(saved,'candidate-a','attempt-a',5000,{q1:'private answer'});
  assert.deepEqual(readAssessmentDraft(saved,'candidate-b','attempt-a',5000,4000),{});
  assert.deepEqual(readAssessmentDraft(saved,'candidate-a','attempt-b',5000,4000),{});
});

test('local draft cannot extend a server deadline or survive completion', () => {
  const saved=storage();writeAssessmentDraft(saved,'candidate-a','attempt-a',5000,{q1:'o1'});
  assert.deepEqual(readAssessmentDraft(saved,'candidate-a','attempt-a',5000,5000),{});
  writeAssessmentDraft(saved,'candidate-a','attempt-a',7000,{q1:'o2'});
  removeAssessmentDraft(saved,'candidate-a','attempt-a');
  assert.deepEqual(readAssessmentDraft(saved,'candidate-a','attempt-a',7000,4000),{});
});

test('a mismatched server deadline invalidates an old draft', () => {
  const saved=storage();writeAssessmentDraft(saved,'candidate-a','attempt-a',5000,{q1:'o1'});
  assert.deepEqual(readAssessmentDraft(saved,'candidate-a','attempt-a',4500,4000),{});
});

test('blocked local storage never prevents test display or submission', () => {
  const blocked={getItem(){throw Error('private mode')},setItem(){throw Error('quota')},removeItem(){throw Error('blocked')}};
  assert.doesNotThrow(()=>writeAssessmentDraft(blocked,'candidate-a','attempt-a',5000,{q1:'o1'}));
  assert.deepEqual(readAssessmentDraft(blocked,'candidate-a','attempt-a',5000,4000),{});
  assert.doesNotThrow(()=>removeAssessmentDraft(blocked,'candidate-a','attempt-a'));
});
