---
rule_kind: validationRule
intent: block creating a second encounter of the same type for the same subject on
  the same day/month
entity_param: encounter
encounter_types: []
concepts: []
source_org: Natco Trust
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  
  const currentEncounterDate = moment(encounter.encounterDateTime).startOf('day');
  const allEncounters = encounter.individual.getEncounters(true);
  
  const sameTypeEncountersToday = allEncounters.filter((enc) => 
  enc.encounterType.name === encounter.encounterType.name &&
  enc.uuid !== encounter.uuid &&
  enc.encounterDateTime !== null &&
  moment(enc.encounterDateTime).startOf('day').isSame(currentEncounterDate) &&
  enc.voided === false
  );
  
  if(sameTypeEncountersToday.length > 0) {
  validationResults.push(imports.common.createValidationError("Attendance entry is already entered for today"));
  }
  
  return validationResults;
 };
```
