import argparse,json,tempfile
from pathlib import Path
from . import asset,vault,loan,privacy
from .report import report,audit

COMMANDS={'asset-import':asset.import_asset,'ownership-import':asset.ownership,
          'appraisal-import':asset.appraise,'vault-import':vault.import_vault,
          'custody-import':vault.custody,'loan-import':loan.originate,
          'payment-import':loan.pay,'extension-import':loan.extend,
          'notice-import':loan.notice,'decision-import':loan.decide}

def main():
    p=argparse.ArgumentParser(description='Offline pledge custody and redemption evidence')
    s=p.add_subparsers(dest='cmd',required=True)
    for name in COMMANDS:
        x=s.add_parser(name);x.add_argument('file');x.add_argument('--db',required=True)
    for name in ('report','audit'):
        x=s.add_parser(name);x.add_argument('--db',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    pr=s.add_parser('privacy-request');pr.add_argument('file');pr.add_argument('--db',required=True)
    pv=s.add_parser('privacy-review');pv.add_argument('request_id');pv.add_argument('--db',required=True)
    args=p.parse_args()
    if args.cmd in COMMANDS:result=COMMANDS[args.cmd](args.db,args.file)
    elif args.cmd=='report':result=report(args.db)
    elif args.cmd=='audit':result=audit(args.db)
    elif args.cmd=='privacy-request':result=privacy.request(args.db,args.file)
    elif args.cmd=='privacy-review':result=privacy.review(args.db,args.request_id)
    else:
        root=Path(__file__).resolve().parent.parent/'fixtures';db=args.db or str(Path(tempfile.mkdtemp())/'demo.sqlite')
        for name,file in [('asset-import','asset-ok.json'),('ownership-import','ownership-ok.json'),
                          ('appraisal-import','appraisal-ok.json'),('vault-import','vault-ok.json'),
                          ('custody-import','custody-ok.json'),('loan-import','loan-ok.json'),
                          ('payment-import','payment-ok.json'),('decision-import','decision-ok.json')]:
            COMMANDS[name](db,root/file)
        result={'db':db,'audit':audit(db),'loan':report(db)['state']['loan']}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
