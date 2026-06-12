---
rule_kind: validationRule
intent: block saving an encounter dated before its scheduled (earliest visit) date
entity_param: encounter
encounter_types: []
concepts: []
source_org: JSS
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const validationResults = [];
  const moment = imports.moment;
  if(encounter.earliestVisitDateTime && moment(encounter.encounterDateTime).isBefore(moment(encounter.earliestVisitDateTime), 'day')) {
  const validationError = imports.common.createValidationError('Cannot save attendance for the future date.');
  validationResults.push(validationError);
  }
  return validationResults;
 };
```
