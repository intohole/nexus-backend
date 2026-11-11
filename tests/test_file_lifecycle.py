import os
import time

from nexus.file_lifecycle import FileFace, quota_check, render_log, run_face, scan_face


def _aged(root, count=6, days=40):
    old = time.time() - days * 86400
    for i in range(count):
        p = root / f"upload_u1_{i}.jpg"
        p.write_bytes(b"x" * (i + 1))
        os.utime(p, (old - i * 86400, old - i * 86400))


def test_dry_run_zero_side_effect(tmp_path):
    root = tmp_path / "shots"
    root.mkdir()
    _aged(root)
    (root / "fresh.jpg").write_bytes(b"y")
    face = FileFace(name="shots", root=root, tier="warm", ttl_hours=24 * 14, keep_n=1)
    r1 = run_face(face, execute=False)
    assert r1["dry"] is True and not r1["archived"], "dry-run 零副作用"
    assert len(list(root.glob("*.jpg"))) == 7, "dry-run 不得动文件"
    assert r1["would_delete"] and r1["bytes_would_free"] > 0


def test_execute_archive_first_then_keep_n(tmp_path):
    root = tmp_path / "shots"
    root.mkdir()
    _aged(root)
    fresh = root / "fresh.jpg"
    fresh.write_bytes(b"y")
    face = FileFace(name="shots", root=root, tier="warm", ttl_hours=24 * 14, keep_n=1)
    r2 = run_face(face, execute=True)
    assert len(r2["archived"]) == 6, f"过期非顶新 6 个应归档，got {len(r2['archived'])}"
    assert fresh.exists(), "新文件保留"
    assert not (root / "upload_u1_5.jpg").exists(), "最老文件已归档移走"
    assert len(list((root / "_archive").iterdir())) == 3, "归档 keep-N=3"
    assert r2["bytes_freed"] > 0 and r2["reasons"]


def test_execute_idempotent_second_round(tmp_path):
    root = tmp_path / "shots"
    root.mkdir()
    _aged(root)
    face = FileFace(name="shots", root=root, tier="warm", ttl_hours=24 * 14, keep_n=1)
    run_face(face, execute=True)
    before = len(list((root / "_archive").iterdir()))
    r = run_face(face, execute=True)
    assert r["archived"] == [] and len(list((root / "_archive").iterdir())) == before


def test_hot_tier_refuses(tmp_path):
    hot = FileFace(name="hot_face", root=tmp_path, tier="hot")
    assert "error" in run_face(hot), "hot 面拒绝接线"


def test_never_globs_whitelist(tmp_path):
    nf = FileFace(name="never", root=tmp_path, tier="warm", never_globs=("*.jpg",))
    assert nf and run_face(nf)["would_delete"] == [], "never_globs 白名单永不删"


def test_consumer_globs_and_skip_predicate(tmp_path):
    root = tmp_path
    old = time.time() - 40 * 86400
    for name in ("a.jpg", "b.dat", "c.tmp"):
        p = root / name
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    face = FileFace(name="f", root=root, tier="warm", ttl_hours=1, keep_n=0,
                    consumer_globs=("*.jpg",), skip_predicate=lambda rel: rel.endswith(".tmp"))
    plan = scan_face(face)
    assert plan["expired"] == ["b.dat"], "仅未命中任何消费面的 b.dat 进过期桶"
    assert sorted(plan["skipped_consumer"]) == ["a.jpg", "c.tmp"]


def test_group_by_parent_keeps_top_per_group(tmp_path):
    root = tmp_path
    old = time.time() - 40 * 86400
    for sub in ("p1", "p2"):
        (root / sub).mkdir()
        for i in range(3):
            p = root / sub / f"{i}.dat"
            p.write_bytes(b"x")
            os.utime(p, (old - i * 86400, old - i * 86400))
    face = FileFace(name="g", root=root, tier="warm", ttl_hours=1, keep_n=1, group_by="parent")
    plan = scan_face(face)
    assert sorted(plan["kept_top"]) == ["p1/0.dat", "p2/0.dat"]
    assert sorted(plan["expired"]) == ["p1/1.dat", "p1/2.dat", "p2/1.dat", "p2/2.dat"]


def test_clock_injection(tmp_path):
    root = tmp_path
    p = root / "a.dat"
    p.write_bytes(b"x")
    face = FileFace(name="c", root=root, tier="warm", ttl_hours=1, keep_n=0, clock=time.time() + 2 * 3600)
    assert run_face(face)["would_delete"] == ["a.dat"], "注入时钟=文件瞬间过期"


def test_archive_dir_override(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    _aged(root, count=2)
    archive = tmp_path / "elsewhere"
    face = FileFace(name="a", root=root, tier="warm", ttl_hours=1, keep_n=0,
                    archive_dir=archive, archive_keep_n=5)
    r = run_face(face, execute=True)
    assert len(r["archived"]) == 2 and len(list(archive.iterdir())) == 2


def test_quota_check_signal_only(tmp_path):
    root = tmp_path
    (root / "big.dat").write_bytes(b"x" * 2048)
    (root / "_archive").mkdir()
    (root / "_archive" / "z.dat").write_bytes(b"x" * 1024)
    face = FileFace(name="q", root=root, tier="warm", quota_warn_mb=0)
    q = quota_check(face)
    assert q["bytes"] == 2048 and q["warn"] is False, "_archive 不计水位"
    hot = FileFace(name="q2", root=root, tier="warm", quota_warn_mb=0.000001)
    q2 = quota_check(hot)
    assert q2["warn"] is True and q2["message"] == "quota_exceeded_signal_only"


def test_render_log_compact_json():
    report = {"face": "x", "dry": True, "archived": []}
    s = render_log(report)
    assert '"dry":true' in s and "\n" not in s
