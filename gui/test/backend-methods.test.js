// Every back-end method the page calls is one gui_service answers. The page names them as strings
// (call('preview', …)), so a method renamed or dropped in Python would otherwise only show up as an
// "unknown method" error in the running app.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');

test('the page calls only methods the back end has', () => {
  const page = read('src/app.js');
  const called = new Set([...page.matchAll(/\bcall\(\s*'([a-z_]+)'/g)].map((m) => m[1]));
  assert.ok(called.size > 5, `found only ${called.size} calls in app.js; has the way it calls changed?`);

  const service = read('../openfilmkit/gui_service.py');
  const listed = service.match(/^METHODS = \(([^)]*)\)/m);
  assert.ok(listed, 'METHODS not found in gui_service.py');
  const methods = new Set([...listed[1].matchAll(/'([a-z_]+)'/g)].map((m) => m[1]));

  const missing = [...called].filter((m) => !methods.has(m));
  assert.deepStrictEqual(missing, [], `app.js calls methods gui_service doesn't have: ${missing.join(', ')}`);
});
