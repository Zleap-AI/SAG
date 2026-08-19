import assert from "node:assert/strict";
import test from "node:test";

import {
  describeUpdaterError,
  shouldPresentUpdaterError,
} from "../src/updater-error.ts";

test("guides signature-mismatched test builds to the official download", () => {
  const presentation = describeUpdaterError(
    new Error(
      "Code signature at URL file:///tmp/SAG.app/ did not pass validation: "
      + "代码未能满足指定的代码要求",
    ),
  );

  assert.equal(presentation.kind, "signature-mismatch");
  assert.match(presentation.message, /无法验证正式更新包的签名/);
  assert.match(presentation.detail, /覆盖安装一次/);
  assert.equal(
    presentation.actionUrl,
    "https://github.com/Zleap-AI/SAG/releases/latest",
  );
});

test("keeps an unexpected updater error visible for diagnosis", () => {
  const presentation = describeUpdaterError(new Error("network connection reset"));

  assert.equal(presentation.kind, "generic");
  assert.match(presentation.detail, /network connection reset/);
  assert.equal(presentation.actionUrl, undefined);
});

test("presents errors after an update download without interrupting background checks", () => {
  assert.equal(shouldPresentUpdaterError({ status: "downloaded" }), true);
  assert.equal(shouldPresentUpdaterError({ status: "checking" }), false);
  assert.equal(shouldPresentUpdaterError({ status: "downloading" }), false);
});
