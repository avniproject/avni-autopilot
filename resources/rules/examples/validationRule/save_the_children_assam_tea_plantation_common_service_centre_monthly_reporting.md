---
rule_kind: validationRule
intent: block saving when one observation count exceeds another logically-larger one
  (e.g. beneficiaries received > beneficiaries requested)
entity_param: encounter
encounter_types: []
concepts:
- Total number beneficiaries who received services at Common Service Centre
- Total number of beneficiaries who requested / accessed services at Common Service
  Centre
source_org: Save the children Assam Tea plantation
---
```js
"use strict";
 ({params, imports}) => {
  const encounter = params.entity;
  const validationResults = [];
  if(encounter.getObservationReadableValue('Total number beneficiaries who received services at Common Service Centre') > encounter.getObservationReadableValue('Total number of beneficiaries who requested / accessed services at Common Service Centre')) {
  validationResults.push(imports.common.createValidationError('Number beneficiaries who received services cannot be greater than number of members who requested/accessed!'));
  } 
  return validationResults;
 };
```
