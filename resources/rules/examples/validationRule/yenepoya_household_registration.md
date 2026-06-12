---
rule_kind: validationRule
intent: block saving when the respondent does not consent to participate
entity_param: individual
encounter_types: []
concepts:
- Respondent agrees to consent
source_org: Yenepoya
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const validationResults = [];
 

  const consent = individual.getObservationReadableValue('Respondent agrees to consent');
  console.log('consent', consent);
  if (_.isEqual(consent, 'No')) {
  validationResults.push(imports.common.createValidationError('User cannot register because the respondent does not agree to the consent'));
  }
  return validationResults;
 };
```
