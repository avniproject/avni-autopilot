---
rule_kind: formElementRule
intent: show this field only when an earlier observation has been filled in, regardless
  of its value
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: Shelter
field_name: Bill Number For Third Phase ?
kind: visibility
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
  let answersToShow = [];
  let validationErrors = [];
  
  const condition11 = new imports.rulesConfig.RuleCondition({programEncounter, formElement}).when.valueInEncounter("be4d774c-013e-439a-bedf-384498efdb3d").defined.matches();
  
  visibility = condition11 ;
  
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility, value, answersToSkip, validationErrors, answersToShow);
};
```
