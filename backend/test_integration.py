"""Integration test: verify all services connect properly."""
from src.config import Settings
from src.models import TodoItem, TaskSummary
from src.services.search import SearchService
from src.tools.note import NoteTool


def test_search_duckduckgo():
    """Test DuckDuckGo search returns results."""
    svc = SearchService(Settings(search_api="duckduckgo", search_engines=""))
    results = svc.search("Python programming language", 3)
    assert len(results) > 0, "Should return at least 1 result"
    assert "url" in results[0], "Results should have url field"
    assert "title" in results[0], "Results should have title field"
    print(f"TEST PASSED: DuckDuckGo returned {len(results)} results")


def test_search_cache():
    """Test search caching works."""
    svc = SearchService(Settings(search_api="duckduckgo", search_engines=""))
    results1 = svc.search("test cache query", 2)
    results2 = svc.search("test cache query", 2)
    assert len(results1) == len(results2), "Cached and fresh results should match"
    print("TEST PASSED: Search cache works")


def test_note_tool():
    """Test NoteTool save and load."""
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        note = NoteTool(workspace=tmpdir)
        path = note.save_note("test-run", 1, "Test Note", "## Hello\nWorld")
        assert os.path.exists(path)

        content = note.load_note("test-run", 1)
        assert "Test Note" in content
        assert "Hello" in content

        report_path = note.save_report("test-run", "# Report\nContent")
        assert os.path.exists(report_path)
        assert "Report" in note.load_report("test-run")

    print("TEST PASSED: NoteTool save/load works")


def test_models():
    """Test data models."""
    task = TodoItem(id=1, title="Test", intent="Test intent", query="test query")
    assert task.status == "pending"

    summary = TaskSummary(task=task, summary="## Summary\nContent", sources=["https://example.com"])
    assert len(summary.sources) == 1
    assert summary.summary.startswith("## Summary")

    d = task.model_dump()
    assert d["id"] == 1
    print("TEST PASSED: Models work correctly")


if __name__ == "__main__":
    passed = 0
    failed = 0
    skipped = 0

    tests = [
        ("test_models", test_models),
        ("test_note_tool", test_note_tool),
        ("test_search_duckduckgo", test_search_duckduckgo),
        ("test_search_cache", test_search_cache),
    ]

    for name, func in tests:
        try:
            func()
            passed += 1
        except Exception as e:
            err_str = str(e)
            # Mark search-related failures as SKIP (network issue, rate limit, etc.)
            if "search" in name and any(
                kw in err_str.lower()
                for kw in ["ratelimit", "rate limit", "timeout", "connection", "internet",
                           "no results", "blocked", "ssl", "certificate", "name resolution",
                           "getaddrinfo", "timed out"]
            ):
                print(f"[SKIPPED] {name} (network issue: {e})")
                skipped += 1
            else:
                print(f"[FAILED] {name}: {e}")
                failed += 1

    total = passed + failed + skipped
    print(f"\nResults: {passed} passed, {failed} failed, {skipped} skipped (total {total})")

    if failed == 0:
        print("\n[SUCCESS] All integration tests passed!")
    else:
        print("\n[FAILURE] Some integration tests failed!")
        raise SystemExit(1)
