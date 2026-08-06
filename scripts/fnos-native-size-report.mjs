#!/usr/bin/env node
import { lstat, readdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const fail = (m) => { throw new Error(`fnos-native-size: ${m}`); };
async function walk(dir, base = dir) { const out=[]; for (const e of await readdir(dir,{withFileTypes:true})) { const p=path.join(dir,e.name); if(e.isDirectory()) out.push(...await walk(p,base)); else if(e.isFile()) out.push({path:path.relative(base,p),bytes:(await stat(p)).size}); else fail(`unsupported ${p}`); } return out; }
async function main() { const args=Object.fromEntries(process.argv.slice(2).reduce((a,v,i,x)=>i%2?[...a,[x[i-1],v]]:a,[])); const limit=args["--platform"] === "x86" ? 298844160 : args["--platform"] === "arm" ? 272629760 : fail("platform must be x86 or arm"); const fpk=await stat(args["--fpk"]); const entries=await walk(args["--rendered"]); const unpacked=entries.reduce((n,e)=>n+e.bytes,0); if(fpk.size>limit) fail("FPK exceeds platform limit"); const report={platform:args["--platform"],fpk_bytes:fpk.size,unpacked_bytes:unpacked,top_paths:entries.sort((a,b)=>b.bytes-a.bytes).slice(0,20)}; await writeFile(args["--output"],`${JSON.stringify(report,null,2)}\n`); }
main().catch(e=>{process.stderr.write(`${e.message}\n`);process.exitCode=1});
