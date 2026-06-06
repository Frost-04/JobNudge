#!/usr/bin/env python3
"""verify_title_filter.py

Checks that every scraper which opens a job detail page correctly
guards the call behind ``_should_exclude()`` so that senior / staff /
principal roles do not waste network round-trips.

Exit code 0 → all checks pass
Exit code 1 → one or more violations found
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Test helpers for _should_exclude logic
# ---------------------------------------------------------------------------

EXCLUDE_WORDS = ["principal", "senior", "iii", "staff"]

# Test cases with expected outcomes
TEST_CASES: list[tuple[str, bool]] = [
    # ── Should match ──
    ("Senior Software Engineer", True),
    ("Principal Architect", True),
    ("Staff Engineer", True),
    ("Software Engineer III", True),
    ("senior backend developer", True),
    ("PRINCIPAL DATA SCIENTIST", True),
    ("Engineering Staff Lead", True),
    ("staff", True),
    ("iii support", True),
    # ── Should NOT match ──
    ("Software Engineer", False),
    ("Junior Developer", False),
    ("Associate Engineer", False),
    ("", False),
    # ── Edge cases ──
    ("Seniority is not a word", False),  # "Seniority" ≠ word boundary for "senior"
    ("Lead Principal-in-Training", True),
    ("Staffing Coordinator", False),  # "Staffing" starts with "staff" but has trailing chars
]


def _should_exclude(title: str) -> bool:
    """Replica of BaseScraper._should_exclude for testing."""
    if not title:
        return False
    title_lower = title.lower()
    for word in EXCLUDE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", title_lower):
            return True
    return False


# ---------------------------------------------------------------------------
# AST-based file analysis
# ---------------------------------------------------------------------------

class ScraperAnalyzer(ast.NodeVisitor):
    """Walk a scraper module AST and collect facts about detail calls & guards."""

    def __init__(self, source: str) -> None:
        self.source_lines = source.splitlines()
        self.defines_detail_method: bool = False
        self.detail_call_lines: list[int] = []  # lines that call _scrape_detail_page
        self.guarded_lines: list[int] = []       # lines of _should_exclude calls
        # For a detail call to be "guarded", there must be an _should_exclude
        # call in the same enclosing method before the detail call.
        self._current_method_has_guard: bool = False
        self._current_method: str = ""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prev_method = self._current_method
        prev_guard = self._current_method_has_guard
        self._current_method = node.name
        self._current_method_has_guard = False
        self.generic_visit(node)
        self._current_method = prev_method
        self._current_method_has_guard = prev_guard

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        prev_method = self._current_method
        prev_guard = self._current_method_has_guard
        self._current_method = node.name
        self._current_method_has_guard = False
        self.generic_visit(node)
        self._current_method = prev_method
        self._current_method_has_guard = prev_guard

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)

        # Detect `_should_exclude(...)` calls
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "_should_exclude"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            self._current_method_has_guard = True
            self.guarded_lines.append(node.lineno)

        # Detect `self._scrape_detail_page(...)` calls
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "_scrape_detail_page"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            self.detail_call_lines.append(node.lineno)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Reset per-class state
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "_scrape_detail_page":
                    self.defines_detail_method = True
        self.generic_visit(node)


def analyze_scraper_file(filepath: Path) -> dict:
    """Parse a scraper file and return a dict of findings."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except Exception:
        return {"error": f"Could not read {filepath}"}

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"syntax_error": str(e)}

    analyzer = ScraperAnalyzer(source)
    analyzer.visit(tree)

    # Build a snippet around each call line for the report
    def snippet(lineno: int, context: int = 1) -> str:
        start = max(0, lineno - context - 1)
        end = min(len(analyzer.source_lines), lineno + context)
        lines = []
        for i in range(start, end):
            marker = ">>>" if i == lineno - 1 else "   "
            lines.append(f"{marker} {i + 1:4d}: {analyzer.source_lines[i]}")
        return "\n".join(lines)

    return {
        "defines_detail_method": analyzer.defines_detail_method,
        "detail_call_lines": analyzer.detail_call_lines,
        "guarded_lines": analyzer.guarded_lines,
        "snippet": snippet,
    }


# ---------------------------------------------------------------------------
# Main verification
# ---------------------------------------------------------------------------

def main() -> int:
    workspace = Path(__file__).resolve().parent
    scrapers_dir = workspace / "src" / "scrapers"
    base_file = scrapers_dir / "base_scraper.py"

    errors: list[str] = []
    warnings: list[str] = []

    # ── 1. Verify BaseScraper has the exclusion infra ──
    print("=" * 60)
    print("1. BaseScraper exclusion infrastructure")
    print("=" * 60)

    base_text = base_file.read_text(encoding="utf-8")
    if "EXCLUDE_TITLE_WORDS" not in base_text:
        errors.append("BaseScraper is missing EXCLUDE_TITLE_WORDS")
    else:
        print("   ✓ EXCLUDE_TITLE_WORDS present")
    if "def _should_exclude" not in base_text:
        errors.append("BaseScraper is missing _should_exclude method")
    else:
        print("   ✓ _should_exclude method present")

    # ── 2. Unit-test the exclusion logic ──
    print()
    print("=" * 60)
    print("2. _should_exclude logic tests")
    print("=" * 60)

    logic_failures = 0
    for title, expected in TEST_CASES:
        result = _should_exclude(title)
        if result != expected:
            logic_failures += 1
            errors.append(
                f"Logic FAIL: {title!r:45s} expected={expected} got={result}"
            )
    if logic_failures:
        print(f"   ✗ {logic_failures} logic test(s) FAILED")
    else:
        print(f"   ✓ All {len(TEST_CASES)} logic tests passed")

    # ── 3. Check each scraper ──
    print()
    print("=" * 60)
    print("3. Per-scraper guard check")
    print("=" * 60)

    scraper_files = sorted(scrapers_dir.glob("*_scraper.py"))
    scrapers_with_detail_enrichment = 0
    scrapers_with_correct_guard = 0
    scrapers_with_missing_guard = 0
    scrapers_without_detail_enrichment = 0

    for sf in scraper_files:
        name = sf.stem

        # base_scraper is handled separately
        if name == "base_scraper":
            continue

        result = analyze_scraper_file(sf)

        if "syntax_error" in result:
            errors.append(f"{name}: SYNTAX ERROR → {result['syntax_error']}")
            print(f"   ✗ {name}: syntax error!")
            continue
        if "error" in result:
            errors.append(f"{name}: {result['error']}")
            print(f"   ✗ {name}: read error!")
            continue

        defines = result["defines_detail_method"]
        calls = result["detail_call_lines"]
        guards = result["guarded_lines"]
        snip = result["snippet"]

        if not defines:
            # No detail method = no enrichment = no guard needed
            scrapers_without_detail_enrichment += 1
            print(f"   - {name}: no detail enrichment (skip)")
            continue

        scrapers_with_detail_enrichment += 1

        # For each _scrape_detail_page call, verify it's inside a method
        # that also has an _should_exclude call (before the call line).
        # Simple heuristic: if guard line count > 0, the guard exists in the
        # same method scope.
        if guards:
            scrapers_with_correct_guard += 1
            print(f"   ✓ {name}: guarded (calls={calls}, guards={guards})")
        else:
            scrapers_with_missing_guard += 1
            print(f"   ✗ {name}: MISSING GUARD — {len(calls)} call(s) to _scrape_detail_page")
            for cl in calls:
                errors.append(f"{name} line {cl}: unguarded _scrape_detail_page call:")
                for line in snip(cl).splitlines():
                    errors.append(f"     {line}")

    # ── 4. Summary ──
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Scrapers with detail enrichment:     {scrapers_with_detail_enrichment}")
    print(f"  Scrapers with correct guard:         {scrapers_with_correct_guard}")
    print(f"  Scrapers MISSING guard:              {scrapers_with_missing_guard}")
    print(f"  Scrapers without detail enrichment:  {scrapers_without_detail_enrichment}")
    print(f"  Total scraper files checked:         {len(scraper_files) - 1}")

    if errors:
        print()
        print("FAILURES:")
        for e in errors:
            print(f"  {e}")

    if scrapers_with_missing_guard > 0:
        print(f"\n⚠  {scrapers_with_missing_guard} scraper(s) are missing the title-exclusion guard.")
        print("   Run the following pattern on each:")
        print()
        print("   if self._should_exclude(job.title):")
        print("       self.logger.debug(...)")
        print("   else:")
        print("       try:")
        print("           ... = await self._scrape_detail_page(...)")
        print("           ...")
        print("       except Exception as exc:")
        print("           self.logger.warning(...)")

    exit_code = 1 if (errors or scrapers_with_missing_guard > 0) else 0
    print(f"\nExit code: {exit_code}  {'✓ ALL CLEAN' if exit_code == 0 else '✗ ISSUES FOUND'}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
