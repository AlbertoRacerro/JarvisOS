import { readFile } from 'node:fs/promises';
import { basename, join, resolve, sep } from 'node:path';
import { FIXTURE_IDS, fixturePhaseAllowed } from './fixture-registry.mjs';

export const PLAN_SCHEMA = 'jarvisos.browser-proof-plan.v1';
export const PLAN_ID_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const ALLOWED_FIXTURES = FIXTURE_IDS;
export const ALLOWED_ROLES = new Set(['button', 'heading', 'region', 'textbox', 'link']);
export const ALLOWED_ATTRIBUTES = new Set(['aria-pressed', 'data-provider-egress-state']);
export const ARTIFACT_MODES = new Set(['full', 'metadata-only']);
const JSON_TYPES = new Set(['string', 'integer', 'boolean', 'array', 'object', 'nullable-string']);
const CONDITION_OPS = new Set(['equals', 'gt']);

const STEP_KEYS = {
  'navigate': ['op','name','route'],
  'reload': ['op','name'],
  'assert-visible': ['op','name','locator'],
  'assert-count': ['op','name','locator','equals'],
  'click': ['op','name','locator'],
  'fill': ['op','name','locator','value'],
  'open-technical-details': ['op','name','locator'],
  'assert-attribute': ['op','name','locator','attribute','equals'],
  'assert-input-empty': ['op','name','locator'],
  'same-origin-get': ['op','name','path','capture'],
  'capture-text': ['op','name','locator','capture','stripPrefix'],
  'capture-attribute': ['op','name','locator','attribute','capture'],
  'capture-json-find': ['op','name','source','arrayPointer','field','equals','capture'],
  'capture-mutating-request-count': ['op','name','capture'],
  'assert-value-equals': ['op','name','left','right'],
  'map-value': ['op','name','source','capture','cases'],
  'assert-text-template': ['op','name','locator','template'],
  'assert-template-in': ['op','name','template','allowed'],
  'assert-text-template-map': ['op','name','locator','source','cases'],
  'assert-json-deep-equals': ['op','name','locator','right'],
  'assert-json-contract': ['op','name','source','pointer','contract'],
  'assert-body-absent': ['op','name','forbidden','caseInsensitive'],
  'assert-no-button-label': ['op','name','pattern','caseInsensitive'],
  'assert-all-attributes-in': ['op','name','locator','attribute','allowed','count'],
  'run-fixture': ['op','name','fixture','phase'],
  'screenshot': ['op','name','file'],
};
export const ALLOWED_OPS = new Set(Object.keys(STEP_KEYS));

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = (message) => { throw new Error(`invalid proof plan: ${message}`); };
const boundedString = (value, name, max = 500) => {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) fail(`${name} must be a non-empty string <= ${max}`);
};
const safeField = (value, name) => {
  if (typeof value !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(value)) fail(`${name} must be a safe field name`);
};
const validateViewport = (viewport) => {
  if (!isObject(viewport)) fail('viewport must be an object');
  for (const key of Object.keys(viewport)) if (!['width','height'].includes(key)) fail(`viewport unknown key ${key}`);
  if (!Number.isInteger(viewport.width) || viewport.width < 320 || viewport.width > 3840) fail('viewport.width must be an integer in 320..3840');
  if (!Number.isInteger(viewport.height) || viewport.height < 240 || viewport.height > 2160) fail('viewport.height must be an integer in 240..2160');
};
const validateReadOnlySameOriginPostPaths = (paths) => {
  if (!Array.isArray(paths) || paths.length < 1 || paths.length > 20) fail('readOnlySameOriginPostPaths must contain 1..20 exact paths');
  if (new Set(paths).size !== paths.length) fail('readOnlySameOriginPostPaths must be unique');
  for (const [index, path] of paths.entries()) {
    boundedString(path, `readOnlySameOriginPostPaths[${index}]`, 300);
    if (!/^\/[A-Za-z0-9_./%:@+-]*$/.test(path) || path.includes('?') || path.includes('#')) fail(`readOnlySameOriginPostPaths[${index}] must be an exact same-origin pathname`);
    const segments = path.split('/').slice(1);
    if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) fail(`readOnlySameOriginPostPaths[${index}] contains an ambiguous path segment`);
  }
};
const validateExpectedConsoleErrors = (items) => {
  if (!Array.isArray(items) || items.length < 1 || items.length > 10) fail('expectedConsoleErrors must contain 1..10 exact entries');
  const seen = new Set();
  for (const [index, item] of items.entries()) {
    if (!isObject(item)) fail(`expectedConsoleErrors[${index}] must be object`);
    for (const key of Object.keys(item)) if (!['text','count'].includes(key)) fail(`expectedConsoleErrors[${index}] unknown key ${key}`);
    boundedString(item.text, `expectedConsoleErrors[${index}].text`, 500);
    if (seen.has(item.text)) fail('expectedConsoleErrors text must be unique');
    seen.add(item.text);
    if (!Number.isInteger(item.count) || item.count < 1 || item.count > 20) fail(`expectedConsoleErrors[${index}].count invalid`);
  }
};

export function validatePlanId(planId) {
  if (typeof planId !== 'string' || !PLAN_ID_RE.test(planId)) fail('unsafe plan id');
  return planId;
}

export function validateLocator(locator, depth = 0) {
  if (!isObject(locator)) fail('locator must be an object');
  if (depth > 4) fail('locator nesting too deep');
  const kind = locator.kind;
  if (kind === 'role') {
    if (!ALLOWED_ROLES.has(locator.role)) fail(`unsupported role ${locator.role}`);
    boundedString(locator.name, 'locator.name', 300);
    if ('exact' in locator && typeof locator.exact !== 'boolean') fail('locator.exact must be boolean');
    if ('regex' in locator && typeof locator.regex !== 'boolean') fail('locator.regex must be boolean');
    if (locator.exact && locator.regex) fail('locator cannot be exact and regex');
    for (const key of Object.keys(locator)) if (!['kind','role','name','exact','regex'].includes(key)) fail(`unknown role locator key ${key}`);
    return;
  }
  if (kind === 'text' || kind === 'label') {
    boundedString(locator.text, 'locator.text', 500);
    if ('exact' in locator && typeof locator.exact !== 'boolean') fail('locator.exact must be boolean');
    if ('regex' in locator && typeof locator.regex !== 'boolean') fail('locator.regex must be boolean');
    if (locator.exact && locator.regex) fail('locator cannot be exact and regex');
    for (const key of Object.keys(locator)) if (!['kind','text','exact','regex'].includes(key)) fail(`unknown ${kind} locator key ${key}`);
    return;
  }
  if (kind === 'testid') {
    if (typeof locator.testid !== 'string' || !/^[A-Za-z0-9:_-]{1,120}$/.test(locator.testid)) fail('unsafe testid');
    for (const key of Object.keys(locator)) if (!['kind','testid'].includes(key)) fail(`unknown testid locator key ${key}`);
    return;
  }
  if (kind === 'css') {
    boundedString(locator.selector, 'locator.selector', 300);
    if (!/^[A-Za-z0-9_\-#.:[\]=\"' >+~()*^$|,]+$/.test(locator.selector)) fail('unsafe css selector vocabulary');
    for (const key of Object.keys(locator)) if (!['kind','selector'].includes(key)) fail(`unknown css locator key ${key}`);
    return;
  }
  if (kind === 'within') {
    validateLocator(locator.parent, depth + 1);
    validateLocator(locator.child, depth + 1);
    if ('first' in locator && typeof locator.first !== 'boolean') fail('locator.first must be boolean');
    for (const key of Object.keys(locator)) if (!['kind','parent','child','first'].includes(key)) fail(`unknown within locator key ${key}`);
    return;
  }
  fail(`unsupported locator kind ${kind}`);
}

function validateValueRef(ref, name) {
  if (!isObject(ref)) fail(`${name} must be object`);
  const keys = Object.keys(ref);
  if (keys.length !== 1) fail(`${name} must have exactly one source`);
  if ('capture' in ref) boundedString(ref.capture, `${name}.capture`, 64);
  else if ('env' in ref) {
    boundedString(ref.env, `${name}.env`, 64);
    if (!['expectedHead','repository'].includes(ref.env)) fail(`unsupported env ref ${ref.env}`);
  } else if ('literal' in ref) {
    if (!['string','number','boolean'].includes(typeof ref.literal) && ref.literal !== null) fail(`${name}.literal unsupported type`);
  } else if ('json' in ref) {
    if (!isObject(ref.json)) fail(`${name}.json must be object`);
    boundedString(ref.json.source, `${name}.json.source`, 64);
    if (typeof ref.json.pointer !== 'string' || !ref.json.pointer.startsWith('/') || ref.json.pointer.length > 300) fail(`${name}.json.pointer invalid`);
    if ('length' in ref.json && typeof ref.json.length !== 'boolean') fail(`${name}.json.length must be boolean`);
    for (const key of Object.keys(ref.json)) if (!['source','pointer','length'].includes(key)) fail(`unknown json ref key ${key}`);
  } else fail(`${name} has unsupported source`);
}

function validateTemplate(template, name) {
  boundedString(template, name, 1000);
  const tokens = [...template.matchAll(/{{([^}]+)}}/g)].map((match) => match[1]);
  for (const token of tokens) {
    if (/^capture:[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(token)) continue;
    if (/^env:(expectedHead|repository)(\|urlencode)?$/.test(token)) continue;
    if (/^json:[A-Za-z][A-Za-z0-9_-]{0,63}:\/[^|}]*(\|(length|urlencode))?$/.test(token)) continue;
    fail(`unsupported template token ${token}`);
  }
}

function validateFieldRule(rule, name, depth = 0) {
  if (!isObject(rule) || depth > 2) fail(`${name} invalid`);
  for (const key of Object.keys(rule)) if (!['type','equals','in','min','minLength','fields'].includes(key)) fail(`${name} unknown key ${key}`);
  if (!JSON_TYPES.has(rule.type)) fail(`${name}.type invalid`);
  if ('equals' in rule && !['string','number','boolean'].includes(typeof rule.equals) && rule.equals !== null) fail(`${name}.equals invalid`);
  if ('in' in rule) {
    if (!Array.isArray(rule.in) || rule.in.length < 1 || rule.in.length > 20) fail(`${name}.in invalid`);
    for (const value of rule.in) if (!['string','number','boolean'].includes(typeof value) && value !== null) fail(`${name}.in value invalid`);
  }
  if ('min' in rule && (rule.type !== 'integer' || !Number.isInteger(rule.min))) fail(`${name}.min requires integer type`);
  if ('minLength' in rule && (!['string','nullable-string'].includes(rule.type) || !Number.isInteger(rule.minLength) || rule.minLength < 0 || rule.minLength > 10000)) fail(`${name}.minLength requires string-like type and bounded non-negative integer`);
  if ('fields' in rule) {
    if (rule.type !== 'object' && rule.type !== 'array') fail(`${name}.fields requires object or array type`);
    validateFields(rule.fields, `${name}.fields`, depth + 1);
  }
}

function validateFields(fields, name, depth = 0) {
  if (!isObject(fields) || Object.keys(fields).length < 1 || Object.keys(fields).length > 30 || depth > 2) fail(`${name} invalid`);
  for (const [field, rule] of Object.entries(fields)) {
    safeField(field, `${name} field`);
    validateFieldRule(rule, `${name}.${field}`, depth);
  }
}

function validateContract(contract, name) {
  if (!isObject(contract)) fail(`${name} must be object`);
  for (const key of Object.keys(contract)) if (!['fields','conditions'].includes(key)) fail(`${name} unknown key ${key}`);
  validateFields(contract.fields, `${name}.fields`);
  if ('conditions' in contract) {
    if (!Array.isArray(contract.conditions) || contract.conditions.length > 20) fail(`${name}.conditions invalid`);
    for (const [index, condition] of contract.conditions.entries()) {
      if (!isObject(condition)) fail(`${name}.conditions[${index}] invalid`);
      for (const key of Object.keys(condition)) if (!['when','then'].includes(key)) fail(`${name}.conditions[${index}] unknown key ${key}`);
      if (!isObject(condition.when)) fail(`${name}.conditions[${index}].when invalid`);
      for (const key of Object.keys(condition.when)) if (!['field','equals'].includes(key)) fail(`${name}.conditions[${index}].when unknown key ${key}`);
      safeField(condition.when.field, `${name}.conditions[${index}].when.field`);
      if (!Object.prototype.hasOwnProperty.call(contract.fields, condition.when.field)) fail(`${name}.conditions[${index}] selector field not declared`);
      if (!['string','number','boolean'].includes(typeof condition.when.equals) && condition.when.equals !== null) fail(`${name}.conditions[${index}].when.equals invalid`);
      if (!Array.isArray(condition.then) || condition.then.length < 1 || condition.then.length > 10) fail(`${name}.conditions[${index}].then invalid`);
      for (const [j, assertion] of condition.then.entries()) {
        if (!isObject(assertion)) fail(`${name}.conditions[${index}].then[${j}] invalid`);
        for (const key of Object.keys(assertion)) if (!['field','op','value'].includes(key)) fail(`${name}.conditions[${index}].then[${j}] unknown key ${key}`);
        safeField(assertion.field, `${name}.conditions[${index}].then[${j}].field`);
        if (!Object.prototype.hasOwnProperty.call(contract.fields, assertion.field)) fail(`${name}.conditions[${index}].then[${j}] field not declared`);
        if (!CONDITION_OPS.has(assertion.op)) fail(`${name}.conditions[${index}].then[${j}].op invalid`);
        if (!['string','number','boolean'].includes(typeof assertion.value) && assertion.value !== null) fail(`${name}.conditions[${index}].then[${j}].value invalid`);
      }
    }
  }
}

const typePasses = (value, type) => {
  if (type === 'string') return typeof value === 'string';
  if (type === 'integer') return Number.isInteger(value);
  if (type === 'boolean') return typeof value === 'boolean';
  if (type === 'array') return Array.isArray(value);
  if (type === 'object') return isObject(value);
  if (type === 'nullable-string') return value === null || typeof value === 'string';
  return false;
};

function checkFields(value, fields, path = '$') {
  if (!isObject(value)) return { pass:false, detail:`${path} must be object` };
  for (const [field, rule] of Object.entries(fields)) {
    if (!Object.prototype.hasOwnProperty.call(value, field)) return { pass:false, detail:`${path}.${field} missing` };
    const actual = value[field];
    if (!typePasses(actual, rule.type)) return { pass:false, detail:`${path}.${field} type mismatch` };
    if ('equals' in rule && actual !== rule.equals) return { pass:false, detail:`${path}.${field} equality mismatch` };
    if ('in' in rule && !rule.in.includes(actual)) return { pass:false, detail:`${path}.${field} outside allowed set` };
    if ('min' in rule && actual < rule.min) return { pass:false, detail:`${path}.${field} below minimum` };
    if ('minLength' in rule && actual !== null && actual.length < rule.minLength) return { pass:false, detail:`${path}.${field} below minimum length` };
    if ('fields' in rule) {
      if (rule.type === 'object') {
        const nested = checkFields(actual, rule.fields, `${path}.${field}`);
        if (!nested.pass) return nested;
      } else {
        for (let i = 0; i < actual.length; i += 1) {
          const nested = checkFields(actual[i], rule.fields, `${path}.${field}[${i}]`);
          if (!nested.pass) return nested;
        }
      }
    }
  }
  return { pass:true, detail:'contract fields valid' };
}

export function checkJsonContract(value, contract) {
  const fields = checkFields(value, contract.fields);
  if (!fields.pass) return fields;
  for (const condition of contract.conditions ?? []) {
    if (value[condition.when.field] !== condition.when.equals) continue;
    for (const assertion of condition.then) {
      const actual = value[assertion.field];
      const pass = assertion.op === 'equals' ? actual === assertion.value : typeof actual === 'number' && actual > assertion.value;
      if (!pass) return { pass:false, detail:`conditional ${assertion.field} ${assertion.op} failed` };
    }
  }
  return { pass:true, detail:'JSON contract satisfied' };
}

export function inputEmptyResult(value) {
  return value === '' ? { pass:true, detail:'input empty' } : { pass:false, detail:'input non-empty' };
}

export function noButtonLabelMatches(labels, pattern, caseInsensitive = false) {
  const re = new RegExp(pattern, caseInsensitive ? 'i' : '');
  return labels.every((label) => !re.test(String(label).trim()));
}

export function validatePlan(plan) {
  if (!isObject(plan)) fail('root must be an object');
  for (const key of Object.keys(plan)) if (!['schema','id','fixture','artifactMode','viewport','forbidMutatingRequests','readOnlySameOriginPostPaths','expectedConsoleErrors','steps'].includes(key)) fail(`unknown root key ${key}`);
  if (plan.schema !== PLAN_SCHEMA) fail('unknown schema');
  validatePlanId(plan.id);
  const fixture = plan.fixture ?? 'none';
  if (!ALLOWED_FIXTURES.has(fixture)) fail(`unknown fixture ${fixture}`);
  const artifactMode = plan.artifactMode ?? 'full';
  if (!ARTIFACT_MODES.has(artifactMode)) fail(`unknown artifact mode ${artifactMode}`);
  if ('viewport' in plan) validateViewport(plan.viewport);
  if ('forbidMutatingRequests' in plan && typeof plan.forbidMutatingRequests !== 'boolean') fail('forbidMutatingRequests must be boolean');
  if ('readOnlySameOriginPostPaths' in plan) validateReadOnlySameOriginPostPaths(plan.readOnlySameOriginPostPaths);
  if ('expectedConsoleErrors' in plan) validateExpectedConsoleErrors(plan.expectedConsoleErrors);
  if (!Array.isArray(plan.steps) || plan.steps.length < 1 || plan.steps.length > 200) fail('steps must contain 1..200 entries');
  for (const [index, step] of plan.steps.entries()) {
    if (!isObject(step)) fail(`step ${index} must be object`);
    if (!ALLOWED_OPS.has(step.op)) fail(`unknown op ${step.op}`);
    for (const key of Object.keys(step)) if (!STEP_KEYS[step.op].includes(key)) fail(`step ${index} unknown key ${key}`);
    if ('name' in step) boundedString(step.name, `step ${index}.name`, 160);
    if ('locator' in step) validateLocator(step.locator);
    if (step.op === 'navigate') {
      if (typeof step.route !== 'string' || !/^\/[A-Za-z0-9_./?=&%:-]*$/.test(step.route) || step.route.length > 300) fail(`step ${index} unsafe route`);
    } else if (step.op === 'assert-count') {
      if (!step.locator || !Number.isInteger(step.equals) || step.equals < 0 || step.equals > 1000) fail(`step ${index} invalid count`);
    } else if (['assert-visible','click','open-technical-details','assert-input-empty'].includes(step.op)) {
      if (!step.locator) fail(`step ${index} locator required`);
    } else if (step.op === 'fill') {
      if (!step.locator) fail(`step ${index} locator required`);
      boundedString(step.value, `step ${index}.value`, 500);
    } else if (step.op === 'assert-attribute') {
      if (!step.locator || !ALLOWED_ATTRIBUTES.has(step.attribute) || typeof step.equals !== 'string') fail(`step ${index} invalid attribute assertion`);
    } else if (step.op === 'same-origin-get') {
      if (typeof step.path !== 'string' || !/^\/[A-Za-z0-9_./?=&%:{}|\-]*$/.test(step.path) || step.path.length > 500) fail(`step ${index} unsafe request path`);
      boundedString(step.capture, `step ${index}.capture`, 64); validateTemplate(step.path, `step ${index}.path`);
    } else if (step.op === 'capture-text') {
      if (!step.locator) fail(`step ${index} locator required`); boundedString(step.capture, `step ${index}.capture`, 64);
      if ('stripPrefix' in step) boundedString(step.stripPrefix, `step ${index}.stripPrefix`, 300);
    } else if (step.op === 'capture-attribute') {
      if (!step.locator || !ALLOWED_ATTRIBUTES.has(step.attribute)) fail(`step ${index} invalid capture attribute`); boundedString(step.capture, `step ${index}.capture`, 64);
    } else if (step.op === 'capture-json-find') {
      boundedString(step.source, `step ${index}.source`, 64); if (typeof step.arrayPointer !== 'string' || !step.arrayPointer.startsWith('/')) fail(`step ${index} arrayPointer invalid`);
      boundedString(step.field, `step ${index}.field`, 100); boundedString(step.equals, `step ${index}.equals`, 300); boundedString(step.capture, `step ${index}.capture`, 64);
    } else if (step.op === 'capture-mutating-request-count') {
      boundedString(step.capture, `step ${index}.capture`, 64);
    } else if (step.op === 'assert-value-equals') {
      validateValueRef(step.left, `step ${index}.left`); validateValueRef(step.right, `step ${index}.right`);
    } else if (step.op === 'map-value') {
      validateValueRef(step.source, `step ${index}.source`); boundedString(step.capture, `step ${index}.capture`, 64);
      if (!isObject(step.cases) || Object.keys(step.cases).length < 1 || Object.keys(step.cases).length > 30) fail(`step ${index} cases invalid`);
      for (const [key, value] of Object.entries(step.cases)) { boundedString(key, `step ${index} case key`, 100); boundedString(value, `step ${index} case value`, 500); }
    } else if (step.op === 'assert-text-template') {
      if (!step.locator) fail(`step ${index} locator required`); validateTemplate(step.template, `step ${index}.template`);
    } else if (step.op === 'assert-template-in') {
      validateTemplate(step.template, `step ${index}.template`);
      if (!Array.isArray(step.allowed) || step.allowed.length < 1 || step.allowed.length > 40) fail(`step ${index} allowed invalid`);
      for (const item of step.allowed) boundedString(item, `step ${index}.allowed`, 300);
    } else if (step.op === 'assert-text-template-map') {
      if (!step.locator) fail(`step ${index} locator required`); validateValueRef(step.source, `step ${index}.source`);
      if (!isObject(step.cases) || Object.keys(step.cases).length < 1 || Object.keys(step.cases).length > 30) fail(`step ${index} cases invalid`);
      for (const [key, value] of Object.entries(step.cases)) { boundedString(key, `step ${index} case key`, 100); validateTemplate(value, `step ${index} case value`); }
    } else if (step.op === 'assert-json-deep-equals') {
      if (!step.locator) fail(`step ${index} locator required`); validateValueRef(step.right, `step ${index}.right`);
    } else if (step.op === 'assert-json-contract') {
      boundedString(step.source, `step ${index}.source`, 64);
      if (typeof step.pointer !== 'string' || !step.pointer.startsWith('/') || step.pointer.length > 300) fail(`step ${index} pointer invalid`);
      validateContract(step.contract, `step ${index}.contract`);
    } else if (step.op === 'assert-body-absent') {
      if (!Array.isArray(step.forbidden) || step.forbidden.length < 1 || step.forbidden.length > 40) fail(`step ${index} forbidden invalid`);
      for (const item of step.forbidden) boundedString(item, `step ${index}.forbidden`, 120);
      if ('caseInsensitive' in step && typeof step.caseInsensitive !== 'boolean') fail(`step ${index} caseInsensitive invalid`);
    } else if (step.op === 'assert-no-button-label') {
      boundedString(step.pattern, `step ${index}.pattern`, 300);
      if ('caseInsensitive' in step && typeof step.caseInsensitive !== 'boolean') fail(`step ${index} caseInsensitive invalid`);
    } else if (step.op === 'assert-all-attributes-in') {
      if (!step.locator || !ALLOWED_ATTRIBUTES.has(step.attribute) || !Array.isArray(step.allowed) || step.allowed.length < 1 || step.allowed.length > 20) fail(`step ${index} attribute set invalid`);
      for (const item of step.allowed) boundedString(item, `step ${index}.allowed`, 120);
      if (!Number.isInteger(step.count) || step.count < 0 || step.count > 1000) fail(`step ${index} count invalid`);
    } else if (step.op === 'run-fixture') {
      if (!ALLOWED_FIXTURES.has(step.fixture) || step.fixture === 'none') fail(`step ${index} fixture invalid`);
      if (!fixturePhaseAllowed(step.fixture, step.phase)) fail(`step ${index} fixture phase invalid`);
      if (step.fixture !== fixture) fail(`step ${index} fixture does not match plan fixture`);
    } else if (step.op === 'screenshot') {
      if (artifactMode !== 'full') fail(`step ${index} screenshot requires full artifact mode`);
      if (typeof step.file !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(step.file)) fail(`step ${index} screenshot name invalid`);
    }
  }
  return plan;
}

export async function loadTrustedPlan(planId, planDir) {
  validatePlanId(planId);
  const root = resolve(planDir);
  const path = resolve(join(root, `${planId}.json`));
  if (!path.startsWith(`${root}${sep}`) || basename(path) !== `${planId}.json`) fail('plan path escaped trusted directory');
  let raw;
  try { raw = await readFile(path, 'utf8'); } catch (error) { throw new Error(`trusted proof plan not found: ${planId}`, { cause: error }); }
  let plan;
  try { plan = JSON.parse(raw); } catch (error) { throw new Error(`trusted proof plan is not valid JSON: ${planId}`, { cause: error }); }
  validatePlan(plan);
  if (plan.id !== planId) fail('plan id does not match trusted filename');
  return plan;
}

export function jsonPointer(root, pointer) {
  if (pointer === '') return root;
  if (typeof pointer !== 'string' || !pointer.startsWith('/')) throw new Error('invalid JSON pointer');
  return pointer.slice(1).split('/').reduce((value, token) => {
    const key = token.replace(/~1/g, '/').replace(/~0/g, '~');
    if (value === null || value === undefined || !Object.prototype.hasOwnProperty.call(value, key)) throw new Error(`JSON pointer missing ${pointer}`);
    return value[key];
  }, root);
}
