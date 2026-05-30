"""Quick script to extract misrouted tickets from benchmark results."""
import json

data = json.load(open("exploration/output/benchmark_prefilter_results.json"))
misroutes = [r for r in data["results"] if not r["support_group_scores"]["leaf_match"]]

print(f"Total misroutes: {len(misroutes)}\n")
for r in misroutes:
    print(f"{r['ticket_id']} | {r['title'][:65]}")
    print(f"  loc={r.get('location','')} | method={r['method']} | cands={r.get('candidates_count',0)}")
    print(f"  actual={r['actual_support_group']}")
    print(f"  pred  ={r['predicted_support_group']}")
    print(f"  rationale={r.get('rationale','')[:120]}")
    print()