const test = require('node:test');
const assert = require('node:assert');
const { add } = require('./add');

test('add(2, 3) returns 5', () => {
  assert.strictEqual(add(2, 3), 5);
});

test('add handles negative numbers', () => {
  assert.strictEqual(add(-4, 1), -3);
});

test('add handles zero', () => {
  assert.strictEqual(add(0, 0), 0);
});
