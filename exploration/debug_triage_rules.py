"""Debug script to understand why triage rules failed for specific tickets."""
import json

# Load benchmark dataset
data = json.load(open("exploration/output/benchmark_dataset.json"))
tickets = {t["ticket_id"]: t for t in data["tickets"]}

# Tickets to debug
debug_ids = [
    "IR10403102",  # HRIS Support Form - eStar → should be Kronos, got Security Engineering
    "IR10403349",  # Unable to logon to estar → should be Kronos, got Security Engineering
    "IR10317914",  # [PCAM] Hardware Repair - Mobile Phone → should be PCAM, got IAM-SSO
    "IR10398870",  # MS Authentiator → should be IAM-SSO, got ATLAS
]

for tid in debug_ids:
    t = tickets.get(tid)
    if not t:
        print(f"\n{'='*60}\n{tid}: NOT FOUND IN DATASET\n{'='*60}")
        continue
    print(f"\n{'='*60}")
    print(f"TICKET: {tid}")
    print(f"Title: {t.get('title', '')}")
    print(f"Location: {t.get('location', '')}")
    print(f"Actual SG: {t.get('actual_support_group', '')}")
    desc = t.get("description", "")
    print(f"Description ({len(desc)} chars):")
    print(desc[:500])
    print(f"{'='*60}")

    # Test keyword matching
    text = f"{t.get('title', '')} {desc}".lower()
    location = (t.get("location", "") or "").lower()
    
    # Check security_engineering_estar_lgh
    sec_kws = ["estar portal", "estar timestamp", "estar workforce", "e-star portal", "e star portal"]
    sec_ctx = ["lancaster", "lgh"]
    sec_neg = ["hris support form"]
    has_sec_primary = any(kw in text for kw in sec_kws)
    has_sec_context = any(kw in text or kw in location for kw in sec_ctx)
    has_sec_negative = any(kw in text for kw in sec_neg)
    print(f"  security_engineering_estar_lgh: primary={has_sec_primary}, context={has_sec_context}, negative={has_sec_negative}")
    
    # Check hris_estar_kronos
    hris_kws = ["hris support form"]
    hris_ctx = ["estar", "e-star", "e star"]
    hris_neg = ["transfer", "reporting change", "network access", "email access", "remove employee", "no longer employee"]
    has_hris_primary = any(kw in text for kw in hris_kws)
    has_hris_context = any(kw in text or kw in location for kw in hris_ctx)
    has_hris_negative = any(kw in text for kw in hris_neg)
    print(f"  hris_estar_kronos: primary={has_hris_primary}, context={has_hris_context}, negative={has_hris_negative}")
    
    # Check estar_login_kronos
    estar_kws = ["estar", "e-star", "e star"]
    estar_ctx = ["log in", "login", "logon", "log on", "sign in", "unable to access", "unable to log", "cannot log", "can't log", "can not log"]
    estar_neg = ["lha.org", "lgh.org:389", "estar portal", "estar timestamp", "estar workforce", "e-star portal", "e star portal", "hris support form"]
    has_estar_primary = any(kw in text for kw in estar_kws)
    has_estar_context = any(kw in text or kw in location for kw in estar_ctx)
    has_estar_negative = any(kw in text for kw in estar_neg)
    print(f"  estar_login_kronos: primary={has_estar_primary}, context={has_estar_context}, negative={has_estar_negative}")
    
    # Check iam_authentication
    iam_kws = ["ms authenticator", "microsoft authenticator", "authenticator app", "authentication loop",
               "ms authentiator", "microsoft authentiator", "prompting for authenticator", "prompting for ms authenticator"]
    iam_neg = ["duo enrollment", "duo setup", "self-enroll"]
    has_iam_primary = any(kw in text for kw in iam_kws)
    has_iam_negative = any(kw in text for kw in iam_neg)
    print(f"  iam_authentication: primary={has_iam_primary}, negative={has_iam_negative}")
    
    # Check security_engineering_lgh_ldap
    ldap_kws = ["lha.org", "lgh.org:389"]
    has_ldap = any(kw in text for kw in ldap_kws)
    print(f"  security_engineering_lgh_ldap: primary={has_ldap}")