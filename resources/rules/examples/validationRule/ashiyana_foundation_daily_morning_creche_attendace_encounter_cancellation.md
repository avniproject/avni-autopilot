---
rule_kind: validationRule
intent: block cancelling an encounter before its scheduled date
entity_param: encounter
encounter_types: []
concepts: []
source_org: Ashiyana Foundation
---
```js
'use strict';
 ({params, imports}) => {
  const encounter = params.entity;
  const validationResults = [];
  const moment = imports.moment;
 

  if ( !(moment(encounter.encounterDateTime).isSameOrAfter(moment(encounter.earliestVisitDateTime), 'day')) ) {
  validationResults.push(imports.common.createValidationError('Cannot cancel future encounter in advance'));
  }
  
  return validationResults;
 };
```
