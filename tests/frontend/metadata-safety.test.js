import assert from "node:assert/strict";
import test from "node:test";
import { metaItem } from "../../pages/dashboard/modules/utils.js";
test("metadata values are text by default; only explicitly trusted markup is rendered", () => {
  assert.match(metaItem("Label", '<img src=x onerror="evil()">'), /&lt;img/);
  assert.doesNotMatch(metaItem("Label", '<img src=x>'), /<img/);
  assert.match(metaItem("<Label>", '<span>safe</span>', true), /&lt;Label&gt;/);
  assert.match(metaItem("Label", '<span>safe</span>', true), /<span>safe<\/span>/);
});
