---
rule_kind: decisionRule
intent: show which clinical complications apply based on the answers selected on the
  form
entity_param: individual
encounter_types: []
concepts: []
source_org: MDSR-MOHA
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const decisions = params.decisions;
  const complicationsBuilder = new imports.rulesConfig.complicationsBuilder({
  individual : individual,
  complicationsConcept: 'Type of death'
  });
  
  complicationsBuilder.addComplication("Suspected Maternal Death")
  .when.valueInRegistration("Timing of death in pregnancy")
  .containsAnyAnswerConceptName("Pregnancy", "During or within 6 weeks of abortion", "In labour or During Delivery", "Within 1 week after delivery", "7-42  days after delivery");
  complicationsBuilder.addComplication("Non-maternal death")
  .when.valueInRegistration("Timing of death in pregnancy")
  .containsAnyAnswerConceptName("None");
  decisions.registrationDecisions.push(complicationsBuilder.getComplications());
  return decisions;
 };
```
