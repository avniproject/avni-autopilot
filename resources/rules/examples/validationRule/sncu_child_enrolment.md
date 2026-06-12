---
rule_kind: validationRule
intent: block enrolment when the subject was registered in or before a cutoff year
  and the enrolment is being recorded after that year (e.g. 2020)
entity_param: programEnrolment
encounter_types: []
concepts: []
source_org: SNCU
---
```js
'use strict';
 ({params, imports}) => {
  const programEnrolment = params.entity;
  const validationResults = [];
  const moment = imports.moment;
  if ( moment(programEnrolment.individual.registrationDate).year() <= 2020 && moment(programEnrolment.enrolmentDateTime).year() > 2020 ) {
  validationResults.push(imports.common.createValidationError('Enrolment date cannot be after 2020'));
  }
  
  
  return validationResults;
 };
```
