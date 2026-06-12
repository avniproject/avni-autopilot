---
rule_kind: validationRule
intent: block saving an encounter when its date is after a previously-completed terminal/endline
  encounter for the same subject (e.g. block a session visit dated after the cohort
  endline)
entity_param: encounter
encounter_types: []
concepts: []
source_org: Durga India
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  
  const cohortEndline = encounter.individual.getEncounters().filter(enc => enc.encounterType.name == 'Cohort Endline' && enc.encounterDateTime != null);
  
  if(cohortEndline && cohortEndline.length > 0){
  const latest = cohortEndline[0];
 

  const cohortEndlineDate = moment(latest.encounterDateTime).startOf('day');
  const sessionVisitDate = moment(encounter.encounterDateTime).startOf('day');
  if(sessionVisitDate.isAfter(cohortEndlineDate, 'day')) {
  validationResults.push(imports.common.createValidationError("Session Visit Date should be less than the Cohart Endline date or cancel this visit."));  
  }
  }
  
  return validationResults;
 };
```
