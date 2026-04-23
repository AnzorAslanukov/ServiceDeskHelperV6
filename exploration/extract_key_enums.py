"""Extract key enum values from the all_enums.json file."""
import json
import os

output_dir = os.path.join(os.path.dirname(__file__), 'output')
data = json.load(open(os.path.join(output_dir, 'all_enums.json'), 'r', encoding='utf-8'))

target = ['IncidentStatusEnum','ServiceRequestStatusEnum','ChangeStatusEnum','ActivityStatusEnum',
          'System.WorkItem.TroubleTicket.ImpactEnum','System.WorkItem.TroubleTicket.UrgencyEnum',
          'ServiceRequestUrgencyEnum','ServiceRequestPriorityEnum',
          'ChangeRiskEnum','Change_Type','Command_Center_List','Confirmed_Resolution',
          'Yes_No_NA','Increments','ChangeCategoryEnum','ChangeImpactEnum']

out = []
for item in data:
    name = item.get('enumName', '')
    if name in target:
        eid = item.get('enumId', '')
        ed_str = item.get('enumData', '[]')
        ed = json.loads(ed_str)
        out.append(f'=== {name} (enumId: {eid}) ===')
        for v in ed:
            dis = ' [DISABLED]' if v.get('Disabled') else ''
            out.append(f'  {v.get("Value","")}  {v.get("Label","")}{dis}')
            for c in v.get('Children', []):
                cdis = ' [DISABLED]' if c.get('Disabled') else ''
                out.append(f'    {c.get("Value","")}  {c.get("Label","")}{cdis}')
        out.append('')

outpath = os.path.join(output_dir, 'key_enums.txt')
with open(outpath, 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print(f'Written {len(out)} lines to {outpath}')