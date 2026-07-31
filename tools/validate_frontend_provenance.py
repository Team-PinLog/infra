#!/usr/bin/env python3
import argparse, json, re, sys
SHA=re.compile(r"[0-9a-f]{40}"); FP=re.compile(r"[0-9a-f]{20}"); DIGEST=re.compile(r"sha256:[0-9a-f]{64}")
TAG=re.compile(r"([0-9a-f]{40})-cfg-([0-9a-f]{20})-run-([1-9][0-9]*)-a([1-9][0-9]*)")
KEYS={"schema_version","source_repository","source_sha","source_ref","image_repository","image_tag","image_digest","config_fingerprint","workflow_run_id","workflow_run_attempt"}
def fail(m): raise SystemExit(m)
def select_run(a):
 body=json.load(sys.stdin); trusted=[]
 for r in body.get("workflow_runs",[]):
  if r.get("head_sha")==a.source_sha and r.get("head_branch")=="dev" and r.get("event") in {"push","workflow_dispatch"} and r.get("status")=="completed" and r.get("conclusion")=="success" and r.get("path")==".github/workflows/ci.yml" and r.get("repository",{}).get("full_name")=="Team-PinLog/front" and type(r.get("id")) is int and r["id"]>0: trusted.append(r)
 if not trusted: fail("no trusted successful run for current head")
 print(max(trusted,key=lambda r:r["id"])["id"])
def validate(a):
 v=json.loads(open(a.path,encoding="utf-8").read())
 if not isinstance(v,dict) or set(v)!=KEYS: fail("provenance schema keys differ")
 if type(v["schema_version"]) is not int or v["schema_version"]!=1 or type(v["workflow_run_id"]) is not int or type(v["workflow_run_attempt"]) is not int: fail("invalid schema types")
 if any(type(v[k]) is not str for k in KEYS-{"schema_version","workflow_run_id","workflow_run_attempt"}): fail("invalid schema types")
 if v["source_repository"]!="Team-PinLog/front" or v["source_ref"]!="dev" or v["image_repository"]!="ghcr.io/team-pinlog/front": fail("untrusted source")
 m=TAG.fullmatch(v["image_tag"])
 if not m or len(v["image_tag"])>128 or not SHA.fullmatch(v["source_sha"]) or not FP.fullmatch(v["config_fingerprint"]): fail("invalid identity")
 if m.groups()!=(v["source_sha"],v["config_fingerprint"],str(v["workflow_run_id"]),str(v["workflow_run_attempt"])): fail("tag coupling mismatch")
 if not DIGEST.fullmatch(v["image_digest"]): fail("invalid digest")
 if v["workflow_run_id"]!=int(a.run_id) or v["workflow_run_attempt"]!=int(a.run_attempt): fail("run mismatch")
 if v["source_sha"]!=a.source_sha or v["image_digest"]!=a.registry_digest: fail("external mismatch")
 print(v["image_tag"]);print(v["image_digest"]);print(v["source_sha"])
def main():
 p=argparse.ArgumentParser();s=p.add_subparsers(required=True);q=s.add_parser("select-run");q.add_argument("--source-sha",required=True);q.set_defaults(fn=select_run)
 q=s.add_parser("validate");q.add_argument("path");q.add_argument("--run-id",required=True);q.add_argument("--run-attempt",required=True);q.add_argument("--source-sha",required=True);q.add_argument("--registry-digest",required=True);q.set_defaults(fn=validate)
 a=p.parse_args();
 if not SHA.fullmatch(a.source_sha): fail("invalid source sha")
 a.fn(a)
if __name__=="__main__": main()
