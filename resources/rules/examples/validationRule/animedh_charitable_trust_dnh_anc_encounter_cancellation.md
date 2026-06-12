---
rule_kind: validationRule
intent: block cancelling an encounter more than N days before its scheduled date (skip
  the check once the program is exited)
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: Animedh Charitable Trust DNH
---
```js
'use strict';
 ({params, imports}) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  let cancelDateTime = moment(programEncounter.cancelDateTime).format('YYYY-MM-DD');
  let earliestVisitDateTime = moment(programEncounter.earliestVisitDateTime).subtract(5,'days').format('YYYY-MM-DD');
 

  if ( !programEncounter.programEnrolment.programExitDateTime && !( cancelDateTime >= earliestVisitDateTime ) ) {
  validationResults.push(imports.common.createValidationError("This visit cannot be cancelled sooner"));
  };
  
  return validationResults;
 };
```
