---
rule_kind: validationRule
intent: block saving when none of a required set of registration values is provided
  (e.g. at least one of Father/Mother/Guardian name)
entity_param: individual
encounter_types: []
concepts: []
source_org: Gubbachi Pilot
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  
  const condition11 = new imports.rulesConfig.RuleCondition({individual}).when.valueInRegistration("b3c4d5e6-f7a8-9012-6789-012345678001").notDefined.and.when.valueInRegistration("2200ed81-6207-4439-88a6-7647f62c93ec").notDefined.and.when.valueInRegistration("d8e191a4-ba55-49fc-a7f0-5b5e301cb45a").notDefined.matches();
  
  if(condition11 ){
  validationResults.push(imports.common.createValidationError("Either Father, Mother or Guardian's name should be present"));  
 }
  
  return validationResults;
 };
```
