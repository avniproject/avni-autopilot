---
rule_kind: formElementRule
intent: show this field inside a repeating section only when another value within
  the same row meets a numeric condition (e.g. show 'Marks for Q11' only when 'Total
  questions' is between 11 and 20)
entity_param: encounter
encounter_types: []
concepts: []
source_org: Collectives for Integrated Livelihood Initiatives (CInI)
field_name: Marks Obtained in Question 11
kind: visibility
---
```js
'use strict';
({params, imports}) => {
  const encounter = params.entity;
  const moment = imports.moment;
  const formElement = params.formElement;
  const _ = imports.lodash;
  let visibility = true;
  let value = null;
  let answersToSkip = [];
  let validationErrors = [];
  
  const condition11 = new imports.rulesConfig.RuleCondition({encounter, formElement}).when.questionGroupValueInEncounter("20978e88-ee4d-468a-a774-2ae8d9e6e5af", "b0cb420a-3cec-4d21-8f5f-53c28de7052a", params.questionGroupIndex).greaterThanOrEqualTo(11).and.when.questionGroupValueInEncounter("20978e88-ee4d-468a-a774-2ae8d9e6e5af", "b0cb420a-3cec-4d21-8f5f-53c28de7052a", params.questionGroupIndex).lessThanOrEqualTo(20).matches();
  
  visibility = condition11 ;
  
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility, value, answersToSkip, validationErrors);
};
```
