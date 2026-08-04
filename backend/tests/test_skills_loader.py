"""Skills loader: SKILL.md discovery, frontmatter parsing, mention resolution.

Pure-stdlib module (no PyYAML) — these tests build fake SKILL.md trees in
``tmp_path`` so nothing on disk is touched.
"""
from __future__ import annotations

from pathlib import Path

from app.agents.context_fragments import ContextFragment
from app.agents.skills.loader import (
    MAX_BODY_BYTES,
    Skill,
    _parse_frontmatter,
    load_skills,
    resolve_mentions,
    skill_fragment,
)


# --------------------------------------------------------------------------- #
# _parse_frontmatter
# --------------------------------------------------------------------------- #
def test_parse_frontmatter_plain_values():
    text = "---\nname: git-commit\ndescription: Conventional commit helper\n---\nbody here\n"
    meta, body = _parse_frontmatter(text)
    assert meta["name"] == "git-commit"
    assert meta["description"] == "Conventional commit helper"
    assert body == "body here"


def test_parse_frontmatter_quoted_values():
    text = (
        "---\n"
        'name: "quoted-name"\n'
        "description: 'it has: a colon inside'\n"
        "---\n"
        "# Body\n"
        "text\n"
    )
    meta, body = _parse_frontmatter(text)
    assert meta["name"] == "quoted-name"
    assert meta["description"] == "it has: a colon inside"
    assert body == "# Body\ntext"


def test_parse_frontmatter_none_present_returns_original():
    text = "# Just a title\n\nno frontmatter here\n"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    # Body is the original text (splitlines/rejoin may drop a trailing newline
    # only when frontmatter exists; with no frontmatter we return text as-is).
    assert body == text


def test_parse_frontmatter_empty_string():
    meta, body = _parse_frontmatter("")
    assert meta == {}
    assert body == ""


def test_parse_frontmatter_unclosed_block_treated_as_none():
    # Opener present but no closer -> lenient: whole text is body, no meta.
    text = "---\nname: orphan\nthis is not closed\n"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text


# --------------------------------------------------------------------------- #
# load_skills
# --------------------------------------------------------------------------- #
def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_load_skills_from_multiple_roots(tmp_path):
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    _write(
        r1 / "a" / "SKILL.md",
        "---\nname: alpha\ndescription: first\n---\nalpha body\n",
    )
    _write(
        r1 / "b" / "SKILL.md",
        "---\nname: beta\ndescription: keep\n---\nbeta body\n",
    )
    _write(
        r2 / "c" / "SKILL.md",
        "---\nname: gamma\ndescription: from r2\n---\ngamma body\n",
    )

    skills = load_skills([r1, r2])
    assert set(skills) == {"alpha", "beta", "gamma"}
    assert skills["alpha"].body == "alpha body"
    assert skills["alpha"].source == (r1 / "a" / "SKILL.md")
    assert skills["gamma"].source == (r2 / "c" / "SKILL.md")


def test_load_skills_later_root_overrides_on_name_clash(tmp_path):
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    _write(r1 / "SKILL.md", "---\nname: shared\ndescription: old\n---\nOLD BODY\n")
    _write(r2 / "SKILL.md", "---\nname: shared\ndescription: new\n---\nNEW BODY\n")

    skills = load_skills([r1, r2])
    assert list(skills) == ["shared"]
    assert skills["shared"].body == "NEW BODY"
    assert skills["shared"].description == "new"
    assert skills["shared"].source == (r2 / "SKILL.md")


def test_load_skills_skips_files_missing_name(tmp_path):
    root = tmp_path / "root"
    _write(root / "good" / "SKILL.md", "---\nname: kept\n---\nbody\n")
    # No name key at all.
    _write(root / "bad" / "SKILL.md", "---\ndescription: no name\n---\nbody\n")
    # Empty name.
    _write(root / "empty" / "SKILL.md", "---\nname:   \n---\nbody\n")
    # No frontmatter.
    _write(root / "nofm" / "SKILL.md", "just markdown, no frontmatter\n")

    skills = load_skills([root])
    assert list(skills) == ["kept"]


def test_load_skills_body_byte_cap_truncates(tmp_path):
    root = tmp_path / "root"
    # Build a body comfortably over the cap.
    big = "X" * (MAX_BODY_BYTES + 500)
    _write(root / "SKILL.md", f"---\nname: big\n---\n{big}\n")

    skills = load_skills([root])
    body = skills["big"].body
    # Truncated to at most MAX_BODY_BYTES of UTF-8.
    assert len(body.encode("utf-8")) <= MAX_BODY_BYTES
    assert len(body.encode("utf-8")) == MAX_BODY_BYTES  # exact for ASCII
    # And it's valid UTF-8 (decode didn't leave a partial codepoint).
    body.encode("utf-8").decode("utf-8")


def test_load_skills_ignores_missing_or_nonexistent_roots(tmp_path):
    real_root = tmp_path / "real"
    _write(real_root / "SKILL.md", "---\nname: only\n---\nbody\n")
    ghost = tmp_path / "does-not-exist"
    skills = load_skills([ghost, real_root])
    assert list(skills) == ["only"]


# --------------------------------------------------------------------------- #
# resolve_mentions
# --------------------------------------------------------------------------- #
def test_resolve_bare_dollar_mention():
    skills = {"git-commit": Skill("git-commit", "d", "body", Path("x"))}
    out = resolve_mentions("please use $git-commit now", skills)
    assert [s.name for s in out] == ["git-commit"]


def test_resolve_markdown_link_mention():
    skills = {"git-commit": Skill("git-commit", "d", "body", Path("x"))}
    out = resolve_mentions("see [$git-commit](skill://anything) for details", skills)
    assert [s.name for s in out] == ["git-commit"]


def test_resolve_dedups_preserving_first_seen_order():
    skills = {
        "alpha": Skill("alpha", "d", "b", Path("x")),
        "beta": Skill("beta", "d", "b", Path("x")),
    }
    text = "$beta and $alpha then $beta again and $alpha too"
    out = resolve_mentions(text, skills)
    # First-seen order is beta, alpha; repeats dropped.
    assert [s.name for s in out] == ["beta", "alpha"]


def test_resolve_ignores_unknown_mentions():
    skills = {"git-commit": Skill("git-commit", "d", "body", Path("x"))}
    out = resolve_mentions("$git-commit and $nope and $also-missing", skills)
    assert [s.name for s in out] == ["git-commit"]


def test_resolve_mixed_link_and_bare_dedup_to_one():
    skills = {"git-commit": Skill("git-commit", "d", "body", Path("x"))}
    text = "[$git-commit](skill://x) plus a bare $git-commit"
    out = resolve_mentions(text, skills)
    assert [s.name for s in out] == ["git-commit"]


def test_resolve_empty_text_returns_empty():
    out = resolve_mentions("", {"x": Skill("x", "d", "b", Path("p"))})
    assert out == []


# --------------------------------------------------------------------------- #
# skill_fragment
# --------------------------------------------------------------------------- #
def test_skill_fragment_shape():
    skill = Skill("git-commit", "Conventional commits", "step 1\nstep 2\n", Path("p"))
    frag = skill_fragment(skill)
    assert isinstance(frag, ContextFragment)
    assert frag.name == "skill"
    assert frag.tag == "skill"
    assert frag.body == "# Skill: git-commit\nstep 1\nstep 2\n"


def test_skill_fragment_renders_tagged_block():
    skill = Skill("x", "d", "do the thing", Path("p"))
    rendered = skill_fragment(skill).render()
    assert rendered.startswith("<skill>\n")
    assert rendered.endswith("\n</skill>")
    assert "# Skill: x" in rendered
    assert "do the thing" in rendered
