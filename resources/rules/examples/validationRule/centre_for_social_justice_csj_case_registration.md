---
rule_kind: validationRule
intent: lock the entry date after the first save so it can't be changed on subsequent
  edits
entity_param: individual
encounter_types: []
concepts:
- 7b1a1540-bfd5-484d-987f-4d6f3635a448
source_org: Centre for Social Justice (CSJ)
---
```js
"use strict";
 ({ params, imports }) => {
  const individual = params.entity;
  const moment = imports.moment;
  const validationResults = [];
 

  const registrationDateForEditCase = individual.getObservationValue("7b1a1540-bfd5-484d-987f-4d6f3635a448");
  const registrationDate = individual.registrationDate;
 

  if (!registrationDateForEditCase) {
  if (registrationDate) {
  const today = moment().startOf('day');
  const regDate = moment(registrationDate).startOf('day');
 

  if (!regDate.isSame(today, 'day')) {
  validationResults.push(
  imports.common.createValidationError("Registration Date should be today's date.")
  );
  }
  }
  } else {
  const regDateEdit = moment(registrationDate).startOf('day');
  const regDateOriginal = moment(registrationDateForEditCase).startOf('day');
 

  if (!regDateEdit.isSame(regDateOriginal, 'day')) {
  validationResults.push(
  imports.common.createValidationError("Registration Date cannot be changed.")
  );
  }
  }
 

  return validationResults;
 };
```
