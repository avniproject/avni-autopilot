---
rule_kind: validationRule
intent: block saving when the visit date is earlier than the treatment-start date
  for this enrolment
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: CES
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const validationResults = [];
  
  let treatmentStartDate = programEncounter.programEnrolment
  .findObservationInEntireEnrolment('Treatment start date').getValue();
  let visitDate = programEncounter.encounterDateTime; 
  
  //console.log('treatmentStartDate',treatmentStartDate);
  //console.log('visitDate',visitDate);
  
  
  if(imports.moment(visitDate).isSameOrBefore(treatmentStartDate, 'date')) {
  validationResults
  .push(imports.common
  .createValidationError('IP/CP check in cannot be before Start treatment date'));
  }
  
  return validationResults;
 };
```
