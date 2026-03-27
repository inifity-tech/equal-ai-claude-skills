import json, sys, re

def grade(filepath):
    with open(filepath) as f:
        content = f.read()
    content_lower = content.lower()
    
    results = []
    
    # 1. reads-architecture-docs: mentions docs/architecture or architecture docs
    has_arch_docs = bool(re.search(r'docs/architecture|architecture doc|LLD|HLD', content, re.IGNORECASE))
    results.append({
        "text": "reads-architecture-docs",
        "passed": has_arch_docs,
        "evidence": "References architecture docs" if has_arch_docs else "No reference to architecture docs"
    })
    
    # 2. validates-docs-vs-code: explicit discrepancy section or calls out doc-code mismatch
    has_discrepancies = bool(re.search(r'discrepanc|doc.*vs.*code|docs.*don.t match|doc.*says.*but.*code|doc.*claims.*but', content, re.IGNORECASE))
    results.append({
        "text": "validates-docs-vs-code",
        "passed": has_discrepancies,
        "evidence": "Explicitly validates docs against code" if has_discrepancies else "No doc-vs-code validation"
    })
    
    # 3. references-specific-files: contains actual file paths
    file_refs = re.findall(r'[\w/-]+\.(?:py|ts|md|yaml|json|toml)', content)
    has_files = len(file_refs) >= 3
    results.append({
        "text": "references-specific-files",
        "passed": has_files,
        "evidence": f"References {len(file_refs)} specific files" if has_files else f"Only {len(file_refs)} file references"
    })
    
    # 4. web-research-performed: contains URLs or mentions of web sources
    urls = re.findall(r'https?://[^\s\)]+', content)
    has_research = len(urls) >= 2
    results.append({
        "text": "web-research-performed",
        "passed": has_research,
        "evidence": f"Includes {len(urls)} source URLs" if has_research else f"Only {len(urls)} URLs found"
    })
    
    # 5. cites-versions: mentions version numbers
    versions = re.findall(r'(?:version|v)\s*[\d]+[\.\d]+|>=?\s*[\d]+\.[\d]+\.\d+|\d+\.\d+\.\d+', content, re.IGNORECASE)
    has_versions = len(versions) >= 2
    results.append({
        "text": "cites-versions",
        "passed": has_versions,
        "evidence": f"Cites {len(versions)} version references" if has_versions else f"Only {len(versions)} version references"
    })
    
    # 6. asks-probing-questions: count question marks in dedicated questions section
    questions_section = re.search(r'(?:Questions|Drive|Discussion|Deeper|Explore).*?(?=\n---|\n## |$)', content, re.DOTALL | re.IGNORECASE)
    if questions_section:
        q_marks = questions_section.group().count('?')
        numbered_qs = len(re.findall(r'^\d+\.', questions_section.group(), re.MULTILINE))
    else:
        q_marks = content.count('?')
        numbered_qs = 0
    has_questions = numbered_qs >= 5 or q_marks >= 8
    results.append({
        "text": "asks-probing-questions",
        "passed": has_questions,
        "evidence": f"{numbered_qs} numbered questions, {q_marks} question marks in section" if has_questions else f"Only {numbered_qs} numbered questions"
    })
    
    # 7. questions-cover-edge-cases: questions mention edge cases, failure, race, timeout, etc
    edge_patterns = ['edge case', 'failure', 'race', 'timeout', 'what happens when', 'what happens if', 'silently', 'zombie', 'infinite', 'deadlock', 'bottleneck', 'die', 'crash', 'drop', 'lost', 'missing', 'stale']
    edge_hits = sum(1 for p in edge_patterns if p in content_lower)
    has_edge = edge_hits >= 3
    results.append({
        "text": "questions-cover-edge-cases",
        "passed": has_edge,
        "evidence": f"Covers {edge_hits} edge case/failure patterns" if has_edge else f"Only {edge_hits} edge case patterns"
    })
    
    # 8. opinionated-take: has a clear recommendation section with opinion language
    opinion_patterns = ["i'd", "i would", "my take", "my recommendation", "i'd lean", "i'd push", "i'd prioritize", "i'd focus", "here's where i land", "the biggest"]
    opinion_hits = sum(1 for p in opinion_patterns if p in content_lower)
    has_opinion = opinion_hits >= 2
    results.append({
        "text": "opinionated-take",
        "passed": has_opinion,
        "evidence": f"Uses {opinion_hits} opinion/recommendation phrases" if has_opinion else f"Only {opinion_hits} opinion phrases"
    })
    
    # 9. conversational-tone: uses first person, contractions, casual phrasing
    casual_patterns = ["here's what", "let me", "i noticed", "i see", "i've", "you're", "you've", "looking at this", "so i've", "honestly", "the thing is", "worth", "interesting"]
    casual_hits = sum(1 for p in casual_patterns if p in content_lower)
    is_conversational = casual_hits >= 4
    results.append({
        "text": "conversational-tone",
        "passed": is_conversational,
        "evidence": f"Uses {casual_hits} conversational phrases" if is_conversational else f"Only {casual_hits} conversational phrases - too formal"
    })
    
    return {"expectations": results, "pass_rate": sum(1 for r in results if r["passed"]) / len(results)}

if __name__ == "__main__":
    result = grade(sys.argv[1])
    print(json.dumps(result, indent=2))
