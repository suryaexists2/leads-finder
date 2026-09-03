"""
View and manage scraped leads in terminal.

Usage:
    python leads.py                # show all leads
    python leads.py --with-email   # only leads with email
    python leads.py --stats        # show statistics
    python leads.py --export       # export to leads_export.txt
"""
import csv
import os
import sys
import argparse

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LEADS_CSV = os.path.join(DATA_DIR, "leads.csv")


def load():
    if not os.path.exists(LEADS_CSV):
        return []
    with open(LEADS_CSV, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def show(leads):
    if not leads:
        print("\n  [!] No leads found. Run: python scrape.py")
        return
    print(f"\n{'='*70}")
    print(f"  {'#':>3}  {'Name':<28} {'Email':<30} {'Keyword':<20}")
    print(f"{'='*70}")
    for i, l in enumerate(leads, 1):
        name = (l.get("name") or "?")[:26]
        email = (l.get("email") or "-")[:28]
        kw = (l.get("source_keyword") or "")[:18]
        print(f"  {i:>3}  {name:<28} {email:<30} {kw:<20}")
    print(f"{'='*70}")
    print(f"  Total: {len(leads)}")
    with_email = sum(1 for l in leads if (l.get("email") or "").strip())
    print(f"  With email: {with_email}")
    print()


def stats(leads):
    if not leads:
        print("\n  [!] No leads found.")
        return
    by_kw = {}
    by_status = {}
    with_email = 0
    for l in leads:
        kw = l.get("source_keyword", "?")
        by_kw[kw] = by_kw.get(kw, 0) + 1
        st = l.get("status", "new")
        by_status[st] = by_status.get(st, 0) + 1
        if (l.get("email") or "").strip():
            with_email += 1

    print(f"\n  Total leads: {len(leads)}")
    print(f"  With email: {with_email}")
    print(f"\n  By keyword:")
    for kw, count in sorted(by_kw.items(), key=lambda x: -x[1]):
        print(f"    {kw:<40} {count}")
    print(f"\n  By status:")
    for st, count in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"    {st:<20} {count}")
    print()


def export(leads):
    path = os.path.join(DATA_DIR, "leads_export.txt")
    with open(path, "w", encoding="utf-8") as f:
        for l in leads:
            f.write(f"Name: {l.get('name', '?')}\n")
            f.write(f"Profile: {l.get('profile_url', '')}\n")
            f.write(f"Email: {l.get('email', '')}\n")
            f.write(f"Post: {l.get('post_text', '')}\n")
            f.write(f"Keyword: {l.get('source_keyword', '')}\n")
            f.write("-" * 50 + "\n")
    print(f"\n  Exported {len(leads)} leads to {path}\n")


def main():
    parser = argparse.ArgumentParser(description="View LinkedIn leads")
    parser.add_argument("--with-email", action="store_true", help="Only leads with email")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--export", action="store_true", help="Export to text file")
    args = parser.parse_args()

    leads = load()

    if args.with_email:
        leads = [l for l in leads if (l.get("email") or "").strip()]

    if args.stats:
        stats(leads)
    elif args.export:
        export(leads)
    else:
        show(leads)


if __name__ == "__main__":
    main()
