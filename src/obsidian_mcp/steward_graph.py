"""Explicit resolved note graph and metadata navigation, without link insertion."""
from collections import defaultdict

from obsidian_mcp.link_integrity import LinkIndex
from obsidian_mcp.stewardship import Snapshot


def knowledge_map(snapshot: Snapshot, *, scopes: list[str] | None = None,
                  target: str | None = None, limit: int = 100) -> dict:
    if scopes is not None and (not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes)):
        raise ValueError('scopes must be a list of declared scope strings')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Graph page limit must be 1..1000')
    nodes = {p: d for p, d in snapshot.documents.items() if scopes is None or d.properties.get('scope') in scopes}
    index = LinkIndex(snapshot.root, snapshot.policy)
    index.report(include_resolved=True)
    edges, adjacency, incoming, outgoing = [], defaultdict(set), defaultdict(set), defaultdict(set)
    navigation, boundary_edges = defaultdict(list), []
    for row in index.results:
        source, destination = row['source'], row.get('path')
        if row['status'] != 'resolved' or source not in nodes or destination not in nodes:
            continue
        edge = {'source': source, 'target': destination, 'line': row['line'], 'revision': row['revision']}
        edges.append(edge)
        incoming[destination].add(source)
        outgoing[source].add(destination)
        left, right = nodes[source].properties.get('scope'), nodes[destination].properties.get('scope')
        if left != right:
            boundary_edges.append(edge)
            continue
        adjacency[source].add(destination)
        adjacency[destination].add(source)
    for path, doc in sorted(nodes.items()):
        scope = doc.properties.get('scope')
        scope = scope if isinstance(scope, str) else 'undeclared'
        navigation[(scope, 'folder', path.rpartition('/')[0] or '/')].append(path)
        if isinstance(doc.properties.get('type'), str):
            navigation[(scope, 'type', doc.properties['type'])].append(path)
        tags = doc.properties.get('tags', [])
        if isinstance(tags, list):
            for tag in set(t for t in tags if isinstance(t, str)):
                navigation[(scope, 'tag', tag)].append(path)
    remaining, components = set(nodes), []
    while remaining:
        start = min(remaining)
        stack, component = [start], set()
        while stack:
            path = stack.pop()
            if path in component:
                continue
            component.add(path)
            stack.extend(adjacency[path] - component)
        remaining -= component
        components.append(sorted(component))
    isolated = sorted(p for p in nodes if not incoming[p] and not outgoing[p])
    selected_edges = [e for e in edges if target is None or target in {e['source'], e['target']}]
    return snapshot.report([], edges=selected_edges[:limit], edge_count=len(selected_edges), edges_truncated=len(selected_edges) > limit,
                           node_count=len(nodes), components=components, isolated_notes=isolated,
                           navigation=[{'scope': s, 'kind': k, 'value': v, 'notes': paths} for (s, k, v), paths in sorted(navigation.items())],
                           boundary_edges=boundary_edges[:limit], boundary_edge_count=len(boundary_edges),
                           incoming=sorted(incoming.get(target, set()))[:limit] if target else [],
                           outgoing=sorted(outgoing.get(target, set()))[:limit] if target else [],
                           graph_diagnostics=index.diagnostics,
                           complete=not snapshot.diagnostics and not index.diagnostics,
                           coverage_warning='Existing resolved links and explicit folder/type/frontmatter tags only. Isolation is not a defect. Components never join differing scopes. No semantic suggestions or generated notes.')
