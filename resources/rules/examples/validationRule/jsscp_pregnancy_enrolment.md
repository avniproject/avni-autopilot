---
rule_kind: validationRule
intent: block enrolment when Parity exceeds Gravida, the subject is male, or the subject
  is under a minimum age (e.g. 16)
entity_param: programEnrolment
encounter_types: []
concepts:
- Gravida
- Parity
source_org: JSSCP
---
```js
"use strict";
 ({params, imports}) => {
  const validationResults = [];
  
  const programEnrolment = params.entity;
  if(programEnrolment.getObservationReadableValue('Parity') > 
  programEnrolment.getObservationReadableValue('Gravida')) {
  validationResults.push(imports.common.createValidationError('Para Cannot be greater than Gravida'));
  }
  
  // const individual = params.entity;
  if(!programEnrolment.individual.isFemale() ){
  validationResults.push(imports.common.createValidationError('Only female can be enrolled in Pregnancy Program'));
  }
  
  if( programEnrolment.individual.getAgeInYears() < 16){
  validationResults.push(imports.common.createValidationError("Can't enrol in pregnancy program as age is less than 16 years"));
  }
  
  
  return validationResults;
 };
```
