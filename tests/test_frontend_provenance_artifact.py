import stat, subprocess, sys, tempfile, unittest, warnings, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "extract_frontend_provenance_artifact.py"

class ArtifactExtraction(unittest.TestCase):
    def run_zip(self, entries):
        with tempfile.TemporaryDirectory() as d:
            archive, target = Path(d)/"a.zip", Path(d)/"out.json"
            with zipfile.ZipFile(archive, "w") as z:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    for name, data, mode in entries:
                        info=zipfile.ZipInfo(name); info.external_attr=mode << 16
                        z.writestr(info, data)
            result=subprocess.run([sys.executable,str(TOOL),str(archive),str(target)],capture_output=True,text=True)
            return result, target.exists() and target.read_bytes()

    def test_accepts_one_exact_regular_file(self):
        result,data=self.run_zip([("frontend-image-provenance.json",b"{}",stat.S_IFREG|0o600)])
        self.assertEqual(result.returncode,0,result.stderr); self.assertEqual(data,b"{}")

    def test_rejects_malicious_and_ambiguous_archives(self):
        cases=[[("../frontend-image-provenance.json",b"x",stat.S_IFREG|0o600)],[("/frontend-image-provenance.json",b"x",stat.S_IFREG|0o600)],[("dir\\frontend-image-provenance.json",b"x",stat.S_IFREG|0o600)],[("frontend-image-provenance.json/",b"",stat.S_IFDIR|0o700)],[("frontend-image-provenance.json",b"x",stat.S_IFLNK|0o777)],[("frontend-image-provenance.json",b"x",stat.S_IFREG|0o600),("frontend-image-provenance.json",b"y",stat.S_IFREG|0o600)],[("frontend-image-provenance.json",b"x",stat.S_IFREG|0o600),("extra",b"x",stat.S_IFREG|0o600)],[("frontend-image-provenance.json",b"x"*1048577,stat.S_IFREG|0o600)]]
        for entries in cases:
            with self.subTest(entries=entries): self.assertNotEqual(self.run_zip(entries)[0].returncode,0)