---
rule_kind: formElementRule
intent: pre-fill this field with the value of another observation already recorded
  for the subject (e.g. copy phone number from registration)
entity_param: programEncounter
encounter_types: []
concepts:
- ec7d9993-9084-4bde-a211-3ff78f840d21
source_org: Sakhi App
field_name: Mobile number
kind: value
---
```js
'use strict';
({params, imports}) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const formElement = params.formElement;
  const _ = imports.lodash;
  let visibility = true;
  let value = null;
  let answersToSkip = [];
  let validationErrors = [];
  
  const phoneNumber = programEncounter.individual.getObservationValue("ec7d9993-9084-4bde-a211-3ff78f840d21");
  value = phoneNumber;
  
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility, value, answersToSkip, validationErrors);
};
```
