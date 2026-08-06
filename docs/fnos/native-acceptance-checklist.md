# SAG fnOS Native x86 Acceptance Checklist

## Automated evidence

- [x] x86 P0: Python 3.12.4, x86_64, required imports, UDS and fnOS identity headers — [`native-p0-x86.json`](./evidence/native-p0-x86.json).
- [x] Two UID subprocess isolation: separate databases and cross-user source lookup returns 404.
- [x] Native x86 FPK built offline from locked wheels: `276,957,308` bytes, under the `285 MiB` limit.
- [x] Gateway, frontend, lifecycle, data protection and release-workflow tests pass in the branch verification set.

## VMware fnOS x86 operator sign-off

- [ ] Clean install starts without network downloads and opens `/app/sag` over HTTP and HTTPS.
- [ ] Two normal users and one administrator each receive an empty private workspace.
- [ ] Cross-user source, document, agent, thread and attachment IDs return 404.
- [ ] 25 MiB upload, parsing, search SSE and chat SSE complete.
- [ ] Four workers remain usable; fifth user receives bounded 503.
- [ ] Upgrade creates a validated archive; normal uninstall retains data; explicit delete removes only canonical data.
- [ ] Record Gateway, Next and Worker PSS at idle, search and parsing workload.

ARM package acceptance is deliberately deferred until ARM P0 hardware testing is scheduled.

## VMware x86 quick diagnostics

After installing the x86 FPK, run these commands as root and attach their output if launch fails:

```sh
find /vol1/@appdata/sag -maxdepth 3 -type f \( -name '*.log' -o -name '*.pid' \) -print -exec tail -100 {} \;
find /vol1/@appdata/sag -type s -print
ps -ef | grep -E 'sag_api\.fnos\.cli|server\.js|sag_api\.fnos\.worker' | grep -v grep
```

For the installed app, `/app/sag/probe` must report `"status":"pass"` when using the P0 package. For the complete Native package, opening `/app/sag` must render the login/session page; a `502` or blank page should be accompanied by the diagnostics above.
