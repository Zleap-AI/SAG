#!/usr/bin/env node
import { cp, lstat, mkdir, mkdtemp, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { validateNativeTemplate } from "./validate-fnos-native-package.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
const fail = (m) => { throw new Error(`fnos-native-package: ${m}`); };
async function source(from, to) { if ((await lstat(from)).isSymbolicLink()) fail(`source symlink: ${from}`); await cp(from,to,{recursive:true,dereference:false}); }
function args() { const values={}; for(let i=2;i<process.argv.length;i+=2) values[process.argv[i]]=process.argv[i+1]; if(!["x86","arm"].includes(values["--platform"])||!values["--vendor"]||!values["--web"]||!values["--version"]||!values["--output"]) fail("usage: --platform x86|arm --vendor <dir> --web <standalone> --version <version> --output <fpk>"); return values; }
async function main() { const opt=args(); const temp=await mkdtemp(path.join(os.tmpdir(),"sag-native-package-")); try { const rendered=path.join(temp,"sag"); await source(path.join(root,"packages/fnos/native/sag"),rendered); const app=path.join(rendered,"app"); const server=path.join(app,"server"); await mkdir(server,{recursive:true}); await source(path.resolve(opt["--vendor"]),path.join(server,"vendor")); await source(path.join(root,"apps/api/sag_api"),path.join(server,"sag_api")); await source(path.join(root,"apps/api/sag_agent"),path.join(server,"sag_agent")); await source(path.resolve(opt["--web"]),path.join(app,"web")); const staticDir=path.join(root,"apps/web/.next/static"); await source(staticDir,path.join(app,"web/.next/static")); const publicDir=path.join(root,"apps/web/public"); await source(publicDir,path.join(app,"web/public")); const manifest=path.join(rendered,"manifest"); const content=await readFile(manifest,"utf8"); await writeFile(manifest,content.replace("__SAG_VERSION__",opt["--version"]).replace("__SAG_PLATFORM__",opt["--platform"])); await validateNativeTemplate(rendered,opt["--platform"]); const result=spawnSync("fnpack",["build"],{cwd:rendered,encoding:"utf8"}); if(result.status!==0) fail((result.stderr||result.stdout).trim()); await validateNativeTemplate(rendered,opt["--platform"]); await mkdir(path.dirname(path.resolve(opt["--output"])),{recursive:true}); await cp(path.join(rendered,"sag.fpk"),path.resolve(opt["--output"])); } finally { await rm(temp,{recursive:true,force:true}); } }
main().catch(e=>{process.stderr.write(`${e.message}\n`);process.exitCode=1});
