import os
import re
import shlex
import sys


JIRA_KEY_PATTERN = re.compile(r"\bS15P11A705-\d+\b")
EVIDENCE_FIELDS = ("RED", "GREEN", "Regression")
MIN_EVIDENCE_CHARACTERS = 8
EVIDENCE_KEYWORDS = {
    "RED": re.compile(r"fail|failure|error|red|실패|오류", re.IGNORECASE),
    "GREEN": re.compile(r"pass|success|green|통과|성공", re.IGNORECASE),
    "Regression": re.compile(
        r"pass|success|suite|regression|통과|성공|전체|회귀", re.IGNORECASE
    ),
}
COMMAND_PATTERN = re.compile(r"`([^`\n]+)`")
RESULT_PATTERNS = {
    "RED": re.compile(r"\bexit[ \t]*[=:]?[ \t]*[1-9][0-9]*\b", re.IGNORECASE),
    "GREEN": re.compile(r"\bexit[ \t]*[=:]?[ \t]*0\b", re.IGNORECASE),
    "Regression": re.compile(r"\bexit[ \t]*[=:]?[ \t]*0\b", re.IGNORECASE),
}
NEGATED_PATTERNS = {
    "RED": re.compile(
        r"\bno[ \t]+(?:test[ \t]+)?fail(?:ed|ure)?\b|"
        r"\bnot[ \t]+fail(?:ed|ure)?\b|실패[ \t]*(?:없|하지)",
        re.IGNORECASE,
    ),
    "GREEN": re.compile(
        r"\bno[ \t]+(?:test[ \t]+)?pass(?:ed)?\b|"
        r"\bnot[ \t]+pass(?:ed)?\b|(?:통과|성공)[ \t]*(?:없|하지)",
        re.IGNORECASE,
    ),
    "Regression": re.compile(
        r"\b(?:suite[ \t]+)?not[ \t]+run\b|\bnot[ \t]+pass(?:ed)?\b|"
        r"미실행|실행하지|통과하지",
        re.IGNORECASE,
    ),
}


def _is_validation_segment(segment: str) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens:
        return False

    blocked_options = {"-h", "--help", "--version", "--collect-only", "--co"}
    if blocked_options.intersection(tokens):
        return False

    executable = tokens[0].lower()
    if executable in {"python", "python3"}:
        return len(tokens) >= 3 and tokens[1:3] == ["-m", "unittest"]
    if executable == "pytest":
        return True
    if executable in {"go", "cargo"}:
        return len(tokens) >= 2 and tokens[1] == "test"
    if executable == "helm":
        return len(tokens) >= 3 and tokens[1] in {"lint", "template"}
    if executable == "actionlint":
        return True
    if executable == "kubeconform":
        return len(tokens) >= 2
    if executable in {"./gradlew", "gradle"}:
        tasks = [token for token in tokens[1:] if not token.startswith("-")]
        return any(task in {"test", "check"} or task.endswith((":test", ":check")) for task in tasks)
    if executable == "mvn":
        return any(token in {"test", "verify"} for token in tokens[1:])
    if executable in {"npm", "pnpm", "yarn"}:
        if len(tokens) >= 2 and tokens[1] in {"test", "lint"}:
            return True
        return len(tokens) >= 3 and tokens[1] == "run" and tokens[2] in {"test", "lint"}
    return False


def _is_validation_command(command: str) -> bool:
    if any(operator in command for operator in (";", "||", "|", "$(")):
        return False
    segments = [segment.strip() for segment in command.split("&&")]
    return bool(segments) and all(_is_validation_segment(segment) for segment in segments)


def _has_meaningful_evidence(content: str, field: str) -> bool:
    matches = re.finditer(rf"(?mi)^{field}:[ \t]*(.*)$", content)
    for match in matches:
        value = match.group(1).strip()
        meaningful = re.sub(r"[\s`*_>#-]", "", value)
        if (
            len(meaningful) >= MIN_EVIDENCE_CHARACTERS
            and EVIDENCE_KEYWORDS[field].search(value)
            and any(
                _is_validation_command(command)
                for command in COMMAND_PATTERN.findall(value)
            )
            and RESULT_PATTERNS[field].search(value)
            and not NEGATED_PATTERNS[field].search(value)
        ):
            return True
    return False


def validate_pr_body(body: str) -> list[str]:
    content = body or ""
    visible_content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    for fence in ("```", "~~~"):
        visible_content = re.sub(
            rf"(?ms)^[ \t]*{re.escape(fence)}.*?"
            rf"(?:^[ \t]*{re.escape(fence)}[ \t]*$|\Z)",
            "",
            visible_content,
        )
    errors: list[str] = []
    if not JIRA_KEY_PATTERN.search(visible_content):
        errors.append("Jira key is required")
    for field in EVIDENCE_FIELDS:
        if not _has_meaningful_evidence(visible_content, field):
            errors.append(f"{field} evidence is required")
    return errors


def main() -> int:
    errors = validate_pr_body(os.getenv("PR_BODY", ""))
    if errors:
        for error in errors:
            print(error)
        return 1
    print("PR policy validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
