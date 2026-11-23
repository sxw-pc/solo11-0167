import argparse,json,tempfile
from pathlib import Path
from . import asset,vault,loan,dossier
from .report import report,audit,dossier_report

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
    for name in ('report','audit','dossier-report','dossier-audit','dossier-recover'):
        x=s.add_parser(name);x.add_argument('--db',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    pdoc=s.add_parser('dossier-prepare');pdoc.add_argument('file');pdoc.add_argument('--db',required=True);pdoc.add_argument('--out',required=True)
    rdoc=s.add_parser('dossier-review');rdoc.add_argument('file');rdoc.add_argument('--db',required=True)
    cap=s.add_parser('dossier-capture');cap.add_argument('file');cap.add_argument('--db',required=True);cap.add_argument('--out',required=True)
    rel=s.add_parser('dossier-release');rel.add_argument('file');rel.add_argument('--db',required=True);rel.add_argument('--out',required=True)
    ver=s.add_parser('dossier-verify');ver.add_argument('file');ver.add_argument('--db',required=True)
    ack=s.add_parser('dossier-acknowledge');ack.add_argument('file');ack.add_argument('--db',required=True)
    adj=s.add_parser('dossier-adjudicate');adj.add_argument('file');adj.add_argument('--db',required=True)
    args=p.parse_args()
    if args.cmd in COMMANDS:result=COMMANDS[args.cmd](args.db,args.file)
    elif args.cmd=='report':result=report(args.db)
    elif args.cmd=='audit':result=audit(args.db)
    elif args.cmd=='dossier-report':result=dossier_report(args.db)
    elif args.cmd=='dossier-audit':result=dossier.dossier_audit(args.db)
    elif args.cmd=='dossier-recover':result=dossier.recover(args.db)
    elif args.cmd=='dossier-prepare':result=dossier.prepare(args.db,args.file,args.out)
    elif args.cmd=='dossier-review':result=dossier.review(args.db,args.file)
    elif args.cmd=='dossier-capture':result=dossier.capture(args.db,args.file,args.out)
    elif args.cmd=='dossier-release':result=dossier.release(args.db,args.file,args.out)
    elif args.cmd=='dossier-verify':result=dossier.verify_package(args.db,args.file)
    elif args.cmd=='dossier-acknowledge':result=dossier.acknowledge(args.db,args.file)
    elif args.cmd=='dossier-adjudicate':result=dossier.adjudicate(args.db,args.file)
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
