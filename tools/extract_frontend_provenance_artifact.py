#!/usr/bin/env python3
import os, stat, sys, zipfile
from pathlib import Path, PurePosixPath
NAME="frontend-image-provenance.json"; MAX=1024*1024

def main():
 archive,target=Path(sys.argv[1]),Path(sys.argv[2])
 if target.exists(): raise SystemExit("target exists")
 with zipfile.ZipFile(archive) as z:
  infos=z.infolist()
  if len(infos)!=1: raise SystemExit("archive must contain exactly one entry")
  i=infos[0]; p=PurePosixPath(i.filename)
  mode=i.external_attr>>16
  if i.filename!=NAME or p.is_absolute() or ".." in p.parts or "\\" in i.filename or i.is_dir(): raise SystemExit("unsafe entry name")
  if mode and not stat.S_ISREG(mode): raise SystemExit("entry must be regular")
  if i.file_size>MAX or i.compress_size>MAX: raise SystemExit("entry too large")
  data=z.read(i)
  if len(data)!=i.file_size or len(data)>MAX: raise SystemExit("invalid size")
 fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  with os.fdopen(fd,"wb") as f: f.write(data)
 except BaseException:
  target.unlink(missing_ok=True); raise
if __name__=="__main__": main()
