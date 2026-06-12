---
rule_kind: decisionRule
intent: classify multiple clinical risks (anaemia, malnutrition, sickle cell, school
  dropout, mental-health flag) from recorded observation values, and propagate selected
  values to enrolment when the visit is the latest
entity_param: programEncounter
encounter_types: []
concepts:
- 2424293e-2466-4122-970f-716f3019ad55
- 9705f6ad-50e1-4179-aa60-922014d7cc3c
- Is he/she slower than others in learning and understanding new things
- Is his/her behaviour different from others
- Is there any developmental delay or disability seen
- Nutritional status as per WHO
source_org: Adolescent Sewa Rural
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const decisions = params.decisions;
  const moment = imports.moment;
  
  const latest = programEncounter.programEnrolment.lastFulfilledEncounter("Annual Visit - Baseline");
  const isAllChange = latest === undefined || programEncounter.uuid === latest.uuid || moment(programEncounter.encounterDateTime).isAfter(moment(latest.encounterDateTime));
  
  
  const complicationsBuilder = new imports.rulesConfig.complicationsBuilder({
  programEncounter: programEncounter,
  complicationsConcept: "Risk of"
  });
  
  complicationsBuilder
  .addComplication("Severe anaemia")
  .when.valueInEncounter("Haemoglobin")
  .is.lessThan(8);
  complicationsBuilder
  .addComplication("Severely malnourished")
  .when.valueInEncounter("BMI (kg/m²)")
  .is.lessThan(14.5);
  complicationsBuilder
  .addComplication("Sickle cell disease")
  .when.valueInEncounter("Sickling test result")
  .containsAnyAnswerConceptName("Disease"); 
  
  
  const complicationsBuilderForReferingHospital = new imports.rulesConfig.complicationsBuilder({
  programEncounter: programEncounter,
  complicationsConcept: "Refer to hospital immediately for"
  });
 

  complicationsBuilderForReferingHospital
  .addComplication("Severe Anaemia")
  .when.valueInEncounter("Haemoglobin")
  .is.lessThan(8);
 

  complicationsBuilderForReferingHospital
  .addComplication("Moderate Anaemia")
  .when.valueInEncounter("Haemoglobin")
  .is.greaterThanOrEqualTo(8)
  .and.valueInEncounter("Haemoglobin")
  .is.lessThan(11);
 

  complicationsBuilderForReferingHospital
  .addComplication("Severely malnourished")
  .when.valueInEncounter("BMI (kg/m²)")
  .lessThanOrEqualTo(14.5);
 

  complicationsBuilderForReferingHospital
  .addComplication("School dropout")
  .when.valueInEncounter("Going to school")
  .containsAnyAnswerConceptName("Dropped Out");
 

  complicationsBuilderForReferingHospital
  .addComplication("Chronic sickness")
  .when.valueInEncounter("Is there any other condition you want to mention about him/her")
  .containsAnswerConceptNameOtherThan("No problem");
 

  complicationsBuilderForReferingHospital
  .addComplication("Sickle cell disease")
  .when.valueInEncounter("Sickling test result")
  .containsAnswerConceptName("Disease");
 

  complicationsBuilderForReferingHospital
  .addComplication("Menstrual disorders")
  .when.valueInEncounter("Menstrual disorder")
  .containsAnswerConceptNameOtherThan("No problem");
 

  complicationsBuilderForReferingHospital
  .addComplication("Addiction")
  .when.valueInEncounter("Do you have any addiction")
  .containsAnswerConceptNameOtherThan("None");
  
  const anaemicStatusComplicationsBuilder = new imports.rulesConfig.complicationsBuilder({
  programEncounter: programEncounter,
  complicationsConcept: "Anaemic status"
  });
  anaemicStatusComplicationsBuilder
  .addComplication("Severe anaemia")
  .when.valueInEncounter("Haemoglobin")
  .is.lessThan(8);
  
  anaemicStatusComplicationsBuilder
  .addComplication("Moderate anaemia")
  .when.valueInEncounter("Haemoglobin")
  .is.greaterThanOrEqualTo(8)
  .and.valueInEncounter("Haemoglobin")
  .is.lessThan(11);
  
  const isGoingToSchool = new imports.rulesConfig.RuleCondition({programEncounter}).when.valueInEncounter("9705f6ad-50e1-4179-aa60-922014d7cc3c").containsAnswerConceptName("995cfaee-5598-4293-addc-dcb1da5dbcd3").matches();
  
  const isDroppedOut = new imports.rulesConfig.RuleCondition({programEncounter}).when.valueInEncounter("9705f6ad-50e1-4179-aa60-922014d7cc3c").containsAnswerConceptName("fb1080b4-d1ec-4c87-a10d-3838ba9abc5b").matches();
  
  if(isGoingToSchool && isAllChange){
  decisions.enrolmentDecisions.push({name: "Current Standard", value: programEncounter.getObservationReadableValue('2424293e-2466-4122-970f-716f3019ad55')});
  }
  else if(isDroppedOut && isAllChange){
  decisions.enrolmentDecisions.push({name: "Current Standard", value: programEncounter.getObservationReadableValue('9705f6ad-50e1-4179-aa60-922014d7cc3c')});
  }
  
  const statusAsPerWho = programEncounter.getObservationReadableValue("Nutritional status as per WHO");
  
  if(statusAsPerWho){
  decisions.encounterDecisions.push({name: "Nutritional status as per WHO", value:statusAsPerWho });
  if(isAllChange){
  decisions.enrolmentDecisions.push({name: "Nutritional status as per WHO", value:statusAsPerWho });
  }  
  }
  
  const behaviour = programEncounter.getObservationReadableValue("Is his/her behaviour different from others") === 'Yes';
  const slower = programEncounter.getObservationReadableValue("Is he/she slower than others in learning and understanding new things") === 'Yes';
  const delay = programEncounter.getObservationReadableValue("Is there any developmental delay or disability seen") === 'Yes';
  const mentalHealthConditon = (behaviour || slower || delay)?'Yes':'No';
  decisions.encounterDecisions.push({name: "Mental Health Issue", value:mentalHealthConditon });
  if(isAllChange){
  decisions.enrolmentDecisions.push({name: "Mental Health Issue", value:mentalHealthConditon }); 
  } 
  
  
  decisions.encounterDecisions.push(complicationsBuilder.getComplications(programEncounter));
  decisions.encounterDecisions.push(complicationsBuilderForReferingHospital.getComplications(programEncounter));
  decisions.encounterDecisions.push(anaemicStatusComplicationsBuilder.getComplications(programEncounter));
  return decisions;
 };
```
