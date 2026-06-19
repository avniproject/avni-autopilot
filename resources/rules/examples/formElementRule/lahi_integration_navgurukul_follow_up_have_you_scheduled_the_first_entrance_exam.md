---
rule_kind: formElementRule
intent: show this field only when an earlier question's selected option was a specific
  one
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: lahi Integration
field_name: Have you scheduled the first entrance exam?
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
  let validationErrors = [];
  
  const condition11 = new imports.rulesConfig.RuleCondition({programEncounter, formElement}).when.valueInEncounter("514bcb8b-efc1-4eb3-96bd-4366d4d29874").containsAnswerConceptName("f69d34d8-0316-43b7-b211-653b38ba69d8").matches();
  visibility = condition11 ;
  
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility, value, answersToSkip, validationErrors);
};
```
