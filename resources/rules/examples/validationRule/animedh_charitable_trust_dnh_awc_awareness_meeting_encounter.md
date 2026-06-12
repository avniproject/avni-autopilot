---
rule_kind: validationRule
intent: block saving when the encounter is filled more than N weeks before its scheduled
  date
entity_param: encounter
encounter_types: []
concepts: []
source_org: Animedh Charitable Trust DNH
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const moment = imports.moment;
  const validationResults = [];
 

  if(!encounter.earliestVisitDateTime) return validationResults;
 

  const earliestVisitDateTime = moment(encounter.earliestVisitDateTime);
  const maxVisitDateTime = moment(encounter.maxVisitDateTime);
  const encounterDateTime = moment(encounter.encounterDateTime);
 

 

  const oneWeekBeforeEarliestVisitDateTime = moment(earliestVisitDateTime).subtract(1, 'week');
  if(encounterDateTime.isBefore(oneWeekBeforeEarliestVisitDateTime)){
  validationResults.push(imports.common.createValidationError("This visit cannot be completed sooner"));
  }
 

  return validationResults;
 };
```
