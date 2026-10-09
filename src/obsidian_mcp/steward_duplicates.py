"""Exact note groups and bounded lexical opportunities; no semantic equivalence."""
from collections import Counter, defaultdict
import re

from obsidian_mcp.stewardship import Snapshot, digest


def duplicates(snapshot: Snapshot, *, max_pairs: int = 500, per_note: int = 10,
               threshold: float = 0.7) -> dict:
    if type(max_pairs) is not int or not 1 <= max_pairs <= 5000 or type(per_note) is not int or not 1 <= per_note <= 50:
        raise ValueError('Candidate bounds: max_pairs 1..5000, per_note 1..50')
    if not isinstance(threshold, (float, int)) or not 0 < threshold <= 1:
        raise ValueError('threshold must be in (0,1]')
    exact, normalized, inverted, features = defaultdict(list), defaultdict(list), defaultdict(list), {}
    protected = {}
    for path, doc in sorted(snapshot.documents.items()):
        exact[doc.revision].append(path)
        normalized[digest(re.sub(r'\s+', ' ', doc.text).strip().encode())].append(path)
        words = re.findall(r'\w+', doc.body.casefold())
        # Bounded feature count and posting lengths prevent an all-pairs blowup.
        grams = sorted({' '.join(words[i:i + 5]) for i in range(max(0, len(words) - 4))})[:512]
        features[path] = set(grams)
        protected[path] = {w for w in words if w in {'not', 'no', 'never', 'without'} or any(c.isdigit() for c in w)}
        scope = doc.properties.get('scope')
        if not isinstance(scope, str) or doc.issues:
            continue
        for gram in grams:
            key = (scope, gram)
            if len(inverted[key]) < 100:
                inverted[key].append(path)
    pairs, examined, emitted = [], set(), set()
    for path, doc in sorted(snapshot.documents.items()):
        scope = doc.properties.get('scope')
        if not isinstance(scope, str) or doc.issues:
            continue
        overlap = Counter()
        for gram in features[path]:
            overlap.update(p for p in inverted.get((scope, gram), []) if p != path)
        candidates = sorted(overlap, key=lambda p: (-overlap[p], p))[:per_note]
        for other in candidates:
            key = tuple(sorted((path, other)))
            if key in examined or len(examined) >= max_pairs:
                continue
            examined.add(key)
            second = snapshot.documents[other]
            if doc.revision == second.revision:
                continue
            union = features[path] | features[other]
            similarity = len(features[path] & features[other]) / len(union) if union else 0
            if similarity < threshold:
                continue
            emitted.add(key)
            # Differences are shown, not erased to make a duplication claim.
            differences = {k: [doc.properties.get(k), second.properties.get(k)]
                           for k in doc.properties.keys() | second.properties.keys()
                           if doc.properties.get(k) != second.properties.get(k)}
            pairs.append({'paths': list(key), 'revisions': {path: doc.revision, other: second.revision},
                          'classification': 'uncertain' if protected[path] != protected[other] or differences else 'lexical_overlap',
                          'basis': 'bounded_five_word_shingles', 'similarity': round(similarity, 4),
                          'metadata_differences': differences, 'protected_token_difference': sorted(protected[path] ^ protected[other]),
                          'passages': {path: doc.body[:800], other: second.body[:800]}, 'merge_eligible': False})
    def groups(mapping):
        return [{'fingerprint': key, 'paths': paths, 'revisions': {p: snapshot.documents[p].revision for p in paths}}
                for key, paths in sorted(mapping.items()) if len(paths) > 1]
    return snapshot.report([], exact_groups=groups(exact), normalized_text_groups=groups(normalized), pairs=pairs,
                           candidate_pairs_examined=len(examined), max_pairs=max_pairs, per_note=per_note,
                           candidate_coverage_warning='Posting lists cap at 100, features at 512, candidates are bounded and scope-declared only. Lexical retrieval can miss pairs. Normalized text is not semantic equivalence; pairs are not transitive groups.')
