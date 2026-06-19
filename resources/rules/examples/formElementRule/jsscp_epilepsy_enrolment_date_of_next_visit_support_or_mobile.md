---
rule_kind: formElementRule
intent: block save when the date entered in this field is in the past or future compared
  to today
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: JSSCP
field_name: Date of next visit (Support or Mobile)
kind: validation
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
  
  const condition11 = new imports.rulesConfig.RuleCondition({programEncounter, formElement}).when.valueInEncounter("7a1d6893-f96b-4647-b274-bb78fd369185").lessThan(moment().startOf('day').toDate()).matches();
  
  if(condition11 ){
    validationErrors.push("Date cannot be in the past");  
}
  
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility, value, answersToSkip, validationErrors);
};
```
