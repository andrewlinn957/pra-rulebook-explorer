import json
from pathlib import Path

base=Path('logs/llm-reference-batches')
data=json.load(open(base/'batch-000129.json'))
refs={}

def ref(txt,kind,ident,doc,src,evid,reason='explicit named cross-reference',conf=0.9):
    return [txt,kind,ident,doc,src,evid,reason,conf]

def add(idx,*rs):
    refs.setdefault(data[idx-1]['id'],[]).extend(rs)

def add_pair(indices,*rs):
    for idx in indices: add(idx,*rs)

# SoP3/15 insurance business transfers
add(1, ref('Part VII of FSMA','part','Part VII','FSMA','FSMA','proposed scheme under Part VII of FSMA','explicit statutory Part reference',0.95))
add_pair([8,9],
    ref('section 110 of FSMA','section','section 110','FSMA','FSMA','By virtue of section 110 of FSMA','explicit statutory section reference',0.95),
    ref('Memorandum of Understanding','external','Memorandum of Understanding','Memorandum of Understanding','Bank of England','The Memorandum of Understanding 9 confirms','explicit referenced memorandum',0.9))
add_pair([10,11],
    ref('Memorandum of Understanding','external','Memorandum of Understanding','Memorandum of Understanding','Bank of England','As set out in the Memorandum of Understanding','explicit referenced memorandum',0.9),
    ref('section 109(2)(b) of FSMA','section','section 109(2)(b)','FSMA','FSMA','under section 109(2)(b) of FSMA','explicit statutory section reference',0.95),
    ref('section 109(3)','section','section 109(3)','FSMA','FSMA','under section 109(3)','explicit statutory section reference',0.85),
    ref('Business Transfers Regulations','regulation','Business Transfers Regulations','Business Transfers Regulations','other','notices required under the Business Transfers Regulations','explicit regulation reference',0.9))
add_pair([12,13], ref('section 115 of FSMA','section','section 115','FSMA','FSMA','Under section 115 of FSMA','explicit statutory section reference',0.95))
add_pair([24,25], ref('FSMA','statute','FSMA','FSMA','FSMA','its powers under FSMA','explicit statute reference',0.85))
add_pair([30,31], ref('Part 4A of FSMA','part','Part 4A','FSMA','FSMA','under Part 4A of FSMA','explicit statutory Part reference',0.95))
add_pair([34,35], ref('section 86(3)(b) of the Friendly Societies Act 1992','section','section 86(3)(b)','Friendly Societies Act 1992','other','under section 86(3)(b) of the Friendly Societies Act 1992','explicit statutory section reference',0.95))
add_pair([36,37], ref('section 89 of the Friendly Societies Act 1992','section','section 89','Friendly Societies Act 1992','other','under section 89 of the Friendly Societies Act 1992','explicit statutory section reference',0.95))
add_pair([38,39],
    ref('Schedule 15 to the Friendly Societies Act 1992','section','Schedule 15','Friendly Societies Act 1992','other','Schedule 15 to the Friendly Societies Act 1992 requires','explicit statutory schedule reference',0.95),
    ref('section 88','section','section 88','Friendly Societies Act 1992','other','actuary’s report under section 88','explicit statutory section reference',0.85))
add_pair([40,41],
    ref('4.14','guidance','paragraph 4.14','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','The financial information provided under 4.14','explicit paragraph reference',0.9),
    ref('4.16 to 4.17 below','guidance','paragraphs 4.16 to 4.17','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','4.16 to 4.17 below give further details','explicit paragraph range reference',0.9))
add_pair([52,53], ref('section 92 of the Friendly Societies Act 1992','section','section 92','Friendly Societies Act 1992','other','Under section 92 of the Friendly Societies Act 1992','explicit statutory section reference',0.95))
add_pair([54,55], ref('schedule 15 to the Friendly Societies Act 1992','section','Schedule 15','Friendly Societies Act 1992','other','Under schedule 15 to the Friendly Societies Act 1992','explicit statutory schedule reference',0.95))
add_pair([58,59], ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','The Friendly Societies Act 1992 prescribes','explicit statute reference',0.9))
add_pair([62,63], ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','Under the Friendly Societies Act 1992','explicit statute reference',0.9))
add_pair([66,67],
    ref('Schedule 15 to the Friendly Societies Act 1992','section','Schedule 15','Friendly Societies Act 1992','other','set out in Schedule 15 to the Friendly Societies Act 1992','explicit statutory schedule reference',0.95),
    ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','relevant requirement of the Friendly Societies Act 1992','explicit statute reference',0.9),
    ref('4.13 above','guidance','paragraph 4.13','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','see 4.13 above and 4.28 below','explicit paragraph reference',0.9),
    ref('4.28 below','guidance','paragraph 4.28','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','see 4.13 above and 4.28 below','explicit paragraph reference',0.9),
    ref('Part 4A of FSMA','part','Part 4A','FSMA','FSMA','permissions necessary under Part 4A of FSMA','explicit statutory Part reference',0.95),
    ref('paragraph 15 of Schedule 15 to the FS Act','section','paragraph 15 of Schedule 15','FS Act','other','within scope of paragraph 15 of Schedule 15 to the FS Act','explicit statutory paragraph/schedule reference',0.9),
    ref('FINMA','external','FINMA','FINMA','other','confirmation will be needed from FINMA','explicit foreign regulator reference',0.8))
add_pair([68,69], ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','under the Friendly Societies Act 1992','explicit statute reference',0.9))
add_pair([70,71], ref('Part 4A permission','part','Part 4A','unknown','unknown','If authorisation or a Part 4A permission is needed','explicit Part reference',0.75))
add_pair([72,73], ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','a ‘relevant requirement’ of the Friendly Societies Act 1992','explicit statute reference',0.9))
add_pair([76,77],
    ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','Friendly society transfers are governed by the Friendly Societies Act 1992','explicit statute reference',0.9),
    ref('Part VII of FSMA','part','Part VII','FSMA','FSMA','under Part VII of FSMA','explicit statutory Part reference',0.95),
    ref('section 86(1) of Friendly Societies Act 1992','section','section 86(1)','Friendly Societies Act 1992','other','listed in section 86(1) of Friendly Societies Act 1992','explicit statutory section reference',0.95))
add_pair([78,79], ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','Under the Friendly Societies Act 1992','explicit statute reference',0.9))
add_pair([80,81], ref('4.9','guidance','paragraph 4.9','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','such as that described in 4.9','explicit paragraph reference',0.9))
add_pair([96,97], ref('4.36 above','guidance','paragraph 4.36','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','The procedure in 4.36 above','explicit paragraph reference',0.9))
add_pair([102,103],
    ref('87(1)','section','section 87(1)','Friendly Societies Act 1992','other','if the conditions of 87(1) and 87(2) of the Friendly Societies Act 1992','explicit statutory section reference',0.85),
    ref('87(2) of the Friendly Societies Act 1992','section','section 87(2)','Friendly Societies Act 1992','other','87(1) and 87(2) of the Friendly Societies Act 1992','explicit statutory section reference',0.9),
    ref('87(3)','section','section 87(3)','Friendly Societies Act 1992','other','Where the conditions of 87(1) and 87(3) are met','explicit statutory section reference',0.85))
add_pair([104,105],
    ref('section 88 of the Friendly Societies Act 1992','section','section 88','Friendly Societies Act 1992','other','under section 88 of the Friendly Societies Act 1992','explicit statutory section reference',0.95),
    ref('2.30–2.37 of Chapter 2','guidance','paragraphs 2.30–2.37','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','The general principles in 2.30–2.37 of Chapter 2','explicit paragraph range reference',0.9))
add_pair([106,107], ref('paragraphs 4.5 and 4.6','guidance','paragraphs 4.5 and 4.6','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','reports detailed in paragraphs 4.5 and 4.6','explicit paragraph references',0.9))
add_pair([108,109], ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','Under the Friendly Societies Act 1992','explicit statute reference',0.9))
add_pair([110,111],
    ref('Friendly Societies Act 1992','statute','Friendly Societies Act 1992','Friendly Societies Act 1992','other','Under the Friendly Societies Act 1992','explicit statute reference',0.9),
    ref('4.12 and 4.13','guidance','paragraphs 4.12 and 4.13','SoP3/15 – The Prudential Regulation Authority\'s approach to insurance business transfers','PRA Rulebook','exceptions set out in 4.12 and 4.13','explicit paragraph references',0.9),
    ref('section 86','section','section 86','Friendly Societies Act 1992','other','under section 86','explicit statutory section reference',0.85))
add_pair([112,113], ref('Schedule 3 to the Friendly Societies Act 1992','section','Schedule 3','Friendly Societies Act 1992','other','requirements of Schedule 3 to the Friendly Societies Act 1992','explicit statutory schedule reference',0.95))
add(119, ref('Schedule 15','section','Schedule 15','unknown','unknown','Schedule 15 statement to members','explicit schedule reference in title/text',0.75))
add(123, ref('Part VII of FSMA','part','Part VII','FSMA','FSMA','under Part VII of FSMA','explicit statutory Part reference',0.95))

# SoP3/16 FSCS deposits class
add_pair([128,129], ref('4(1) of the Deposit Guarantee Schemes Regulations 2015','section','4(1)','Deposit Guarantee Schemes Regulations 2015','other','4(1) of the Deposit Guarantee Schemes Regulations 2015','explicit regulation provision reference',0.95))
add_pair([132,133],
    ref('Depositor Protection Part of the PRA Rulebook','part','Depositor Protection','PRA Rulebook','PRA Rulebook','Depositor Protection Part of the PRA Rulebook','explicit PRA Rulebook Part reference',0.95),
    ref('PRA SoP, ‘Deposit Guarantee Scheme\'','guidance','Deposit Guarantee Scheme','PRA SoP','PRA Rulebook','PRA SoP, ‘Deposit Guarantee Scheme\'','explicit statement of policy reference',0.9))
add_pair([134,135], ref('Depositor Protection 42.3','rule','42.3','Depositor Protection','PRA Rulebook','As described in Depositor Protection 42.3','explicit PRA Rulebook rule reference',0.95))
add_pair([136,137],
    ref('Chapter 3','chapter','Chapter 3','SoP3/16 – Calculating risk-based levies for the Financial Services Compensation Scheme deposits class','PRA Rulebook','see Chapter 3','explicit chapter reference',0.85),
    ref('Chapter 4','chapter','Chapter 4','SoP3/16 – Calculating risk-based levies for the Financial Services Compensation Scheme deposits class','PRA Rulebook','see Chapter 4','explicit chapter reference',0.85))
add_pair([140,141], ref('EBA guidelines','guidance','EBA guidelines','EBA guidelines','EBA','specified in the EBA guidelines','explicit EBA guidance reference',0.85))
add_pair([142,143], ref('CRR','regulation','CRR','CRR','CRR','a CRR firm','explicit CRR reference',0.8))
add_pair([144,145],
    ref('Depositor Protection 44.4','rule','44.4','Depositor Protection','PRA Rulebook','provisions of Depositor Protection 44.4','explicit PRA Rulebook rule reference',0.95),
    ref('Depositor Protection 44.2','rule','44.2','Depositor Protection','PRA Rulebook','in accordance with Depositor Protection 44.2','explicit PRA Rulebook rule reference',0.95))
add_pair([146,147],
    ref('paragraph 3.4','guidance','paragraph 3.4','SoP3/16 – Calculating risk-based levies for the Financial Services Compensation Scheme deposits class','PRA Rulebook','calibrated as in paragraph 3.4','explicit paragraph reference',0.9),
    ref('CRR firm','regulation','CRR','CRR','CRR','DGS member that is a CRR firm','explicit CRR reference',0.8))
add_pair([148,149], ref('CRR','regulation','CRR','CRR','CRR','terms used are as defined in the CRR','explicit regulation reference',0.9))
add(150,
    ref('PRA Rulebook','part','PRA Rulebook','PRA Rulebook','PRA Rulebook','calculated as defined in the PRA Rulebook','explicit PRA Rulebook reference',0.85),
    ref('CRR','regulation','CRR','CRR','CRR','CET1 capital to risk-weighted assets 3','footnote defines term by reference to CRR',0.75),
    ref('Article 8 of the CRR','article','Article 8','CRR','CRR','pursuant to Article 8 of the CRR','explicit CRR article reference',0.95),
    ref('form FSA015','form','FSA015','FSA015','PRA Rulebook','report FSA015','explicit regulatory return form reference',0.9),
    ref('FINREP F18.00','template','F18.00','FINREP','EU','FINREP F18.00','explicit FINREP template reference',0.9),
    ref('FINREP F1.00','template','F1.00','FINREP','EU','FINREP F1.00','explicit FINREP template reference',0.9),
    ref('F7.00 returns','template','F7.00','FINREP','EU','F7.00 returns','explicit FINREP template reference',0.85),
    ref('F01.01 (FINREP)','template','F01.01','FINREP','EU','F01.01 (FINREP)','explicit FINREP template reference',0.9),
    ref('F02.00 (FINREP)','template','F02.00','FINREP','EU','F02.00 (FINREP)','explicit FINREP template reference',0.9),
    ref('EBA guidelines on disclosure of encumbered and unencumbered assets','guidance','EBA guidelines on disclosure of encumbered and unencumbered assets','EBA guidelines','EBA','As defined in the EBA guidelines on disclosure of encumbered and unencumbered assets','explicit EBA guidance reference',0.95),
    ref('Depositor Protection Part of the PRA Rulebook','part','Depositor Protection','PRA Rulebook','PRA Rulebook','class A tariff base, as defined in the Depositor Protection Part of the PRA Rulebook','explicit PRA Rulebook Part reference',0.95))

out=base/'batch-000129.compact.jsonl'
with open(out,'w') as f:
    for n in data:
        f.write(json.dumps({'n':n['id'],'r':refs.get(n['id'],[])},ensure_ascii=False,separators=(',',':'))+'\n')
print(out, len(data), sum(len(v) for v in refs.values()))
