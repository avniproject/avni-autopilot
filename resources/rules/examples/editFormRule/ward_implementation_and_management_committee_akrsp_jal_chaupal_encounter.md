---
rule_kind: editFormRule
intent: allow editing the encounter only by the assigned role and only within N weeks
  of the encounter date
entity_param: individual
encounter_types: []
concepts: []
source_org: Ward Implementation and Management Committee - AKRSP
---
```js
"use strict";
 ({params, imports}) => {
  const {entity, form, services, entityContext, myUserGroups, userInfo} = params;
  const moment = imports.moment;
  const userGroups = params.myUserGroups
  .map((grp) => grp.groupName);
  
  const isUserAdministrators = userGroups.includes('Administrators');
  const isUserBlockCoordinator = userGroups.includes('Block Coordinator');
  const isUserAdmin = userGroups.includes('Admin');
  const isUserAnurakshak = userGroups.includes('Anurakshak');
  
  const isEncounterDateWithinFiveWeeks = moment(entity.encounterDateTime).add(5, 'weeks').isSameOrAfter(moment(), 'day');
  
  const output = {
  editable : {
  value: isUserAdministrators || isUserBlockCoordinator || isUserAdmin || (isUserAnurakshak && isEncounterDateWithinFiveWeeks),
  messageKey: 'Edit access denied: Encounter date is past 5 weeks.'
  } 
  }; 
  return output;
 };
```
