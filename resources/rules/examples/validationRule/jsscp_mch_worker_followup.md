---
rule_kind: validationRule
intent: block saving when a reason field is empty, and additionally require evidence
  fields when a yes/no field marks the activity done
entity_param: individual
encounter_types: []
concepts:
- 13af10e4-dd09-4dc1-8e93-aa99bd875d76
- 1f6fc60a-fef4-4235-9bd7-1696ce6bebf9
- ca20965a-9c5d-475d-b4fe-44af22a90ac1
- d18075fb-f382-4595-840f-920a1d39fd45
- f1605d2e-a04d-4b47-ab3c-45ce5f0720fe
source_org: JSSCP
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  
  const audioDescription = individual.getObservationValue("ca20965a-9c5d-475d-b4fe-44af22a90ac1")
  const textDescription = individual.getObservationValue("13af10e4-dd09-4dc1-8e93-aa99bd875d76")
  const audioReason = individual.getObservationValue("f1605d2e-a04d-4b47-ab3c-45ce5f0720fe")
  const textReason = individual.getObservationValue("1f6fc60a-fef4-4235-9bd7-1696ce6bebf9")
  const isFollowupDone = individual.getObservationReadableValue('d18075fb-f382-4595-840f-920a1d39fd45') == 'Yes'
  
  
  if((isFollowupDone && audioDescription == null && textDescription == null) || (audioReason == null && textReason == null)){
  let validationError = imports.common.createValidationError('Please answer "What was the reason for followup" and "Give more details - what should be done" in either text or audio to move forward');
  validationResults.push(validationError);
  }
  
  return validationResults;
 };
```
