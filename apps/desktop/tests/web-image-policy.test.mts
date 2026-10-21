import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const webRoot = fileURLToPath(new URL("../../web/", import.meta.url));

function brandImage(desktop: boolean): { src: string; srcSet?: string } {
  const env = { ...process.env };
  delete env.SAG_DESKTOP_BUILD;
  if (desktop) env.SAG_DESKTOP_BUILD = "true";
  return JSON.parse(execFileSync(process.execPath, ["--input-type=module", "-e", `
    import config from './next.config.mjs';
    import imageProps from 'next/dist/shared/lib/get-img-props.js';
    import imageDefaults from 'next/dist/shared/lib/image-config.js';
    import imageLoader from 'next/dist/shared/lib/image-loader.js';
    const result = imageProps.getImgProps({ src: '/sag-icon.png', alt: '', width: 32, height: 32 }, {
      defaultLoader: imageLoader.default,
      imgConf: { ...imageDefaults.imageConfigDefault, ...config.images },
    });
    console.log(JSON.stringify(result.props));
  `], { cwd: path.resolve(webRoot), env, encoding: "utf8" }));
}

test("desktop brand image bypasses the optimizer that writes into the signed bundle", () => {
  const image = brandImage(true);
  assert.equal(image.src, "/sag-icon.png");
  assert.equal(image.srcSet, undefined);
});

test("ordinary Web deployments retain responsive image optimization", () => {
  const image = brandImage(false);
  assert.match(image.src, /^\/_next\/image\?/);
  assert.match(image.srcSet ?? "", /\/_next\/image\?/);
});
