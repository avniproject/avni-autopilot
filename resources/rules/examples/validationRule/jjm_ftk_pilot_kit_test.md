---
rule_kind: validationRule
intent: block saving today's data when the encounter's scheduled date is still in
  the future
entity_param: encounter
encounter_types: []
concepts: []
source_org: JJM FTK Pilot
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  let earliestVisitDateTime = moment(encounter.earliestVisitDateTime);
  if (moment().startOf('day').isBefore(earliestVisitDateTime.startOf('day'))) {
  let error = imports.common.createValidationError('Please fill in this test only on or after ' + earliestVisitDateTime.format('DD-MMM-YYYY'));
  validationResults.push(error);
  }
  //use imports.common.createValidationError('sample error') to create validation error and push it to validationResults
  return validationResults;
 };
```
