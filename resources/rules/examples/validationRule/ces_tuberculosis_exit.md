---
rule_kind: validationRule
intent: block exiting the program when the recorded date of death is before the enrolment
  date
entity_param: programExit
encounter_types: []
concepts: []
source_org: CES
---
```js
"use strict";
 ({params, imports}) => {
  const programExit = params.entity;
  const validationResults = [];
  
  const deathDate = programExit.findExitObservation('Date of Death').getReadableValue();
  const enrolmentDate = programExit.program.enrolment_date_time;
  console.log('deathDate',deathDate);
  console.log('enrolmentDate',enrolmentDate);
  console.log('programExit',programExit);
  
  
  if(imports.moment(deathDate).isBefore(enrolmentDate, 'date')){
  validationResults.push(imports.common.createValidationError('Date of death cannot be before date of enrolment'));
  }
  
  return validationResults;
 };
```
