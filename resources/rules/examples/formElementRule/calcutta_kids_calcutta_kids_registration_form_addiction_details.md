---
rule_kind: formElementRule
intent: show this field only when the subject's age is above or below a threshold
  (e.g. show child-only details when the subject is under 18)
entity_param: individual
encounter_types: []
concepts: []
source_org: Calcutta Kids
field_name: Addiction Details
kind: visibility
---
```js
'use strict';
({params, imports}) => {
  const individual = params.entity;
  const moment = imports.moment;
  const formElement = params.formElement;
  const _ = imports.lodash;
  let visibility = true;
  let value = null;
  let answersToSkip = [];
  let validationErrors = [];
  
  const condition11 = new imports.rulesConfig.RuleCondition({individual, formElement}).when.ageInYears.greaterThanOrEqualTo(5).matches();
  
  visibility = condition11 ;
  
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility, value, answersToSkip, validationErrors);
};
```
